"""Ask about this article: a conversation grounded in one article, with web search for what it leaves out.

One turn:

1. plan (fast model, JSON): does the question need the web, and which searches (at most three);
2. search through SearXNG and read the best few pages (``websearch``), interleaving the queries' results;
3. answer on the long model, streamed: the article, this turn's web results and the conversation so far.

Web results are numbered across the whole thread (a page found again keeps its number), so an ``[n]`` in an
earlier answer still points at the right source. ``respond`` yields ``Event``s that the web layer turns into
server-sent events; the turn is saved to ``item_chats`` only once the answer is complete.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import prompts, websearch
from pensieve.ai.client import LLMClient, LLMError, approx_tokens, get_client, truncate_to_tokens
from pensieve.ai.common import ai_on, utcnow
from pensieve.ai.memory import profile_text
from pensieve.ai.model_choice import apply_overrides
from pensieve.config import get_settings
from pensieve.db import session_scope
from pensieve.fetch.parse import html_to_text
from pensieve.fetch.reader_mode import extract_reader_html

log = logging.getLogger(__name__)

WORKFLOW_PLAN = "chat_plan"
WORKFLOW_ANSWER = "chat"
QUESTION_MAX_CHARS = 2000
RESULTS_PER_QUERY = 5
PAGES_PER_TURN = 4  # results read in full
SNIPPETS_PER_TURN = 4  # further results the model sees as search snippets only
PAGE_CHARS = 6000
PLAN_HISTORY = 6  # messages the planner sees, newest last
PLAN_OPENING_CHARS = 2500
THIN_ARTICLE_CHARS = 700  # a feed text shorter than this is a link post: read the page itself first
ARTICLE_SHARE = 0.45  # of the long model's input window
WEB_SHARE = 0.35
PROFILE_TOKENS = 600
EARLIER_SOURCES = 30

CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]")
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_PLAN_LOOSE = {
    "type": "object",
    "properties": {"search": {"type": "boolean"}, "queries": {"type": "array"}},
    "required": ["search", "queries"],
}
"""Validation for the plan: a model that writes four queries is trimmed, not failed."""


@dataclass
class Event:
    kind: str
    """status | delta | done | error"""
    data: Any = None


@dataclass
class Numbered:
    n: int
    result: websearch.WebResult


# -- thread ----------------------------------------------------------------------------------------------------


async def load_thread(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID) -> list[dict]:
    chat = await session.get(models.ItemChat, (user_id, item_id))
    return list(chat.messages or []) if chat else []


async def save_turn(
    session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID, question: dict, answer: dict
) -> None:
    chat = await session.get(models.ItemChat, (user_id, item_id), with_for_update=True)
    if chat is None:
        session.add(models.ItemChat(user_id=user_id, item_id=item_id, messages=[question, answer]))
    else:
        chat.messages = [*(chat.messages or []), question, answer]  # a new list, so the JSONB change is seen
    await session.flush()


async def clear_thread(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID) -> None:
    chat = await session.get(models.ItemChat, (user_id, item_id))
    if chat is not None:
        await session.delete(chat)
        await session.flush()


def thread_sources(messages: list[dict]) -> list[dict]:
    """Every web result the thread has numbered, in order."""
    return [s for m in messages if m.get("role") == "assistant" for s in m.get("sources") or []]


def cited_numbers(text: str) -> set[int]:
    out: set[int] = set()
    for match in CITE_RE.finditer(text or ""):
        out.update(int(n) for n in re.split(r"\s*,\s*", match.group(1)))
    return out


# -- article ---------------------------------------------------------------------------------------------------


def article_text(item: models.Item) -> str:
    """The fullest text Pensieve has for the item: the archived page, the reader view or the feed's own text."""
    reader = html_to_text(item.reader_html) if item.reader_html else ""
    return max(item.full_text or "", reader, key=len).strip()


def _article_meta(item: models.Item, feed: models.Feed | None) -> dict[str, str]:
    source = (feed.title if feed and feed.title else "") or websearch.WebResult(item.url or "", "").host
    return {
        "title": item.title or "(untitled)",
        "source": source or "unknown source",
        "url": item.url or "",
        "published": item.published_at.strftime("%-d %B %Y") if item.published_at else "unknown date",
    }


# -- planning and search ---------------------------------------------------------------------------------------


def _clip(text: str, chars: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= chars else text[: chars - 1] + "…"


async def plan_queries(
    client: LLMClient, meta: dict[str, str], text: str, messages: list[dict], question: str
) -> list[str]:
    """The searches this question needs ([] when the article answers it). A failed plan searches the title."""
    settings = get_settings()
    history = [(m.get("role", ""), _clip(m.get("content", ""), 500)) for m in messages[-PLAN_HISTORY:]]
    user = prompts.chat_plan_user(
        utcnow().strftime("%-d %B %Y"),
        meta["title"],
        meta["source"],
        meta["published"],
        text[:PLAN_OPENING_CHARS],
        history,
        question,
    )
    try:
        plan = await client.chat_json(
            settings.llm_fast_model,
            prompts.CHAT_PLAN_SYSTEM,
            user,
            prompts.CHAT_PLAN_SCHEMA,
            max_tokens=400,
            workflow=WORKFLOW_PLAN,
            name="chat_plan",
            validate_with=_PLAN_LOOSE,
        )
    except LLMError as exc:
        log.warning("chat plan failed, searching the title instead: %s", exc)
        return [_clip(meta["title"], 200)]
    if not plan.get("search"):
        return []
    queries: list[str] = []
    for raw in plan.get("queries") or []:
        query = _clip(str(raw), 200)
        if query and query.lower() not in {q.lower() for q in queries}:
            queries.append(query)
    return queries[: prompts.CHAT_MAX_QUERIES] or [_clip(meta["title"], 200)]


def interleave(
    batches: list[list[websearch.WebResult]], exclude: set[str], limit: int
) -> list[websearch.WebResult]:
    """Round-robin over the queries' results (each query's best first), without duplicates or excluded URLs."""
    out: list[websearch.WebResult] = []
    seen = set(exclude)
    for rank in range(max((len(b) for b in batches), default=0)):
        for batch in batches:
            if rank < len(batch):
                key = websearch.canonical(batch[rank].url)
                if key not in seen:
                    seen.add(key)
                    out.append(batch[rank])
    return out[:limit]


def number_results(results: list[websearch.WebResult], earlier: list[dict]) -> list[Numbered]:
    """Thread-wide numbers: a page the thread already cited keeps its number, a new one gets the next."""
    known = {websearch.canonical(s.get("url", "")): int(s["n"]) for s in earlier if s.get("n")}
    nxt = max(known.values(), default=0) + 1
    out: list[Numbered] = []
    for result in results:
        key = websearch.canonical(result.url)
        if key in known:
            out.append(Numbered(known[key], result))
        else:
            known[key] = nxt
            out.append(Numbered(nxt, result))
            nxt += 1
    return out


# -- answer ----------------------------------------------------------------------------------------------------


def visible(raw: str) -> str:
    """What the reader may see of a streamed answer: no <think> blocks, closed or still open."""
    text = _THINK_RE.sub("", raw)
    start = text.find("<think>")
    return text if start == -1 else text[:start]


def _history(messages: list[dict], budget_tokens: int) -> list[dict]:
    """The newest messages that fit the budget, starting on a reader's question."""
    kept: list[dict] = []
    used = 0
    for message in reversed(messages):
        cost = approx_tokens(message.get("content", "")) + 8
        if used + cost > budget_tokens:
            break
        kept.append({"role": message["role"], "content": message.get("content", "")})
        used += cost
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


async def respond(
    user: models.User,
    item_id: uuid.UUID,
    question: str,
    *,
    web: bool,
    client: LLMClient | None = None,
) -> AsyncIterator[Event]:
    """Answer one question about one article, as a stream of events; saves the turn when the answer is whole."""
    client = client or get_client()
    settings = get_settings()
    question = question.strip()[:QUESTION_MAX_CHARS]
    try:
        await apply_overrides()
        async with session_scope() as session:
            item = await session.get(models.Item, item_id)
            if item is None:
                yield Event("error", "That article is gone.")
                return
            feed = await session.get(models.Feed, item.feed_id)
            text = article_text(item)
            if len(text) < THIN_ARTICLE_CHARS and item.url and not item.reader_html:
                yield Event("status", "Reading the full article")
                html = await extract_reader_html(item.url)
                if html and len(html_to_text(html)) > len(text):
                    item.reader_html = html
                    item.reader_fetched_at = utcnow()
                    text = article_text(item)
            messages = await load_thread(session, user.id, item.id)
            profile = await profile_text(session, user.id) if ai_on(user, "memory") else ""
            meta = _article_meta(item, feed)

        earlier = thread_sources(messages)
        searched: list[str] = []
        numbered: list[Numbered] = []
        budget = client.budget_for(settings.llm_long_model)
        if web and websearch.available():
            yield Event("status", "Deciding what to look up")
            searched = await plan_queries(client, meta, text, messages, question)
            if searched:
                yield Event("status", "Searching the web for " + ", ".join(f"“{q}”" for q in searched))
                batches = await asyncio.gather(
                    *(websearch.search(q, limit=RESULTS_PER_QUERY) for q in searched)
                )
                exclude = {websearch.canonical(meta["url"])} if meta["url"] else set()
                found = interleave(list(batches), exclude, PAGES_PER_TURN + SNIPPETS_PER_TURN)
                to_read = found[:PAGES_PER_TURN]
                if to_read:
                    hosts = ", ".join(dict.fromkeys(r.host for r in to_read))
                    yield Event(
                        "status", f"Reading {len(to_read)} page{'s' if len(to_read) != 1 else ''}: {hosts}"
                    )
                    page_chars = min(PAGE_CHARS, int(budget * WEB_SHARE * 4) // max(1, len(to_read)) - 200)
                    await websearch.read_pages(to_read, max_chars=max(page_chars, 800))
                numbered = number_results(found, earlier)

        fresh_numbers = {nr.n for nr in numbered}
        fresh_rows = [
            (
                nr.n,
                nr.result.title,
                nr.result.url,
                nr.result.published or "",
                nr.result.text or nr.result.snippet,
            )
            for nr in numbered
        ]
        earlier_rows = [
            (int(s["n"]), s.get("title", ""), s.get("url", ""))
            for s in earlier
            if s.get("n") and int(s["n"]) not in fresh_numbers
        ][-EARLIER_SOURCES:]
        system = (
            prompts.CHAT_ANSWER_SYSTEM.format(today=utcnow().strftime("%A, %-d %B %Y"))
            + "\n\n"
            + prompts.chat_article_block(
                meta["title"],
                meta["source"],
                meta["url"],
                meta["published"],
                truncate_to_tokens(text, int(budget * ARTICLE_SHARE)),
                truncate_to_tokens(profile, PROFILE_TOKENS) if profile else "",
            )
        )
        final = prompts.chat_question_block(question, fresh_rows, earlier_rows, searched, web)
        room = budget - approx_tokens(system) - approx_tokens(final) - 256
        convo = [
            {"role": "system", "content": system},
            *_history(messages, max(room, 0)),
            {"role": "user", "content": final},
        ]

        yield Event("status", "Thinking")
        model = settings.llm_long_model
        raw = ""
        sent = ""
        async for kind, chunk in client.chat_stream(
            model,
            convo,
            max_tokens=2048,
            workflow=WORKFLOW_ANSWER,
            reasoning=settings.llm_digest_reasoning or None,
            temperature=0.3,
        ):
            if kind != "content":
                continue
            raw += chunk
            shown = visible(raw)
            if len(shown) > len(sent) and shown.startswith(sent):
                yield Event("delta", shown[len(sent) :])
                sent = shown
        answer = visible(raw).strip()
        if not answer:
            yield Event("error", "The model returned an empty answer. Try asking again.")
            return

        asked_at = utcnow().isoformat()
        sources = [
            {
                "n": nr.n,
                "title": nr.result.title,
                "url": nr.result.url,
                "host": nr.result.host,
                "read": bool(nr.result.text),
            }
            for nr in numbered
        ]
        question_msg = {"role": "user", "content": question, "at": asked_at, "web": web}
        answer_msg = {
            "role": "assistant",
            "content": answer,
            "at": utcnow().isoformat(),
            "sources": sources,
            "searched": searched,
            "model": model,
        }
        async with session_scope() as session:
            await save_turn(session, user.id, item_id, question_msg, answer_msg)
        yield Event(
            "done",
            {"question": question_msg, "answer": answer_msg, "thread": [*messages, question_msg, answer_msg]},
        )
    except LLMError as exc:
        log.warning("article chat failed for %s: %s", item_id, exc)
        yield Event("error", "The model didn't answer: " + str(exc)[:200])


__all__ = [
    "Event",
    "article_text",
    "cited_numbers",
    "clear_thread",
    "interleave",
    "load_thread",
    "number_results",
    "plan_queries",
    "respond",
    "save_turn",
    "thread_sources",
    "visible",
]
