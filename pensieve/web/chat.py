"""Ask about this article: the chat panel (reader and paper) and its streamed answers.

``POST /items/{id}/chat`` answers as server-sent events (``status``, ``delta``, ``done`` with the rendered answer,
``error``). The answer runs in a task of its own, so a reader who closes the tab or leaves the paper mid-answer
still finds it in the thread later; the response only relays the task's events, with keep-alive comments while
the model is quiet so no proxy drops the connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.ai import chat, websearch
from pensieve.ai.common import ai_on
from pensieve.models import ItemChat, User
from pensieve.web.items import load_item
from pensieve.web.templating import DB, CsrfUser, CurrentUser, chat_markdown, env, render

log = logging.getLogger(__name__)
router = APIRouter()

STARTERS = (
    "Explain this simply",
    "What's the background?",
    "What has happened since?",
    "Who are the key players?",
)
KEEPALIVE_S = 10.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

_RUNNING: dict[tuple[uuid.UUID, uuid.UUID], asyncio.Task] = {}
"""Answers being written, by (user, item); the task holds itself here until it finishes."""


def chat_view(thread: list[dict]) -> list[dict[str, Any]]:
    """Messages ready for the template: rendered answers, the sources each one cites, the rest it found."""
    by_n = {int(s["n"]): s for s in chat.thread_sources(thread) if s.get("n")}
    out: list[dict[str, Any]] = []
    for msg in thread:
        if msg.get("role") != "assistant":
            out.append({"role": "user", "content": msg.get("content", "")})
            continue
        content = msg.get("content", "")
        cited = chat.cited_numbers(content)
        out.append(
            {
                "role": "assistant",
                "html": chat_markdown(content, by_n),
                "cited": [by_n[n] for n in sorted(cited) if n in by_n],
                "other": [s for s in msg.get("sources") or [] if int(s.get("n") or 0) not in cited],
                "searched": msg.get("searched") or [],
            }
        )
    return out


def web_default(user: User) -> bool:
    return bool((user.settings or {}).get("chat_web", True))


async def _panel_ctx(session: AsyncSession, user: User, item, *, collapsed: bool = False) -> dict[str, Any]:
    thread = await chat.load_thread(session, user.id, item.id)
    return {
        "collapsed": collapsed,
        "item": item,
        "thread": chat_view(thread),
        "enabled": ai_on(user, "chat"),
        "search_on": websearch.available(),
        "web_default": web_default(user),
        "starters": STARTERS,
        "running": (user.id, item.id) in _RUNNING,
    }


@router.get("/items/{item_id}/chat")
async def panel(request: Request, item_id: uuid.UUID, user: CurrentUser, session: DB, collapsed: int = 0):
    """The panel; ``collapsed=1`` (an article opened with a thread) shows only its head until asked for."""
    item, _feed = await load_item(session, user, item_id)
    ctx = await _panel_ctx(session, user, item, collapsed=bool(collapsed))
    return render(request, "partials/chat.html", ctx, user=user)


@router.post("/items/{item_id}/chat/clear")
async def clear(request: Request, item_id: uuid.UUID, user: CsrfUser, session: DB):
    item, _feed = await load_item(session, user, item_id)
    await chat.clear_thread(session, user.id, item.id)
    await session.commit()
    return render(request, "partials/chat.html", await _panel_ctx(session, user, item), user=user)


def sse(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _answer_html(thread: list[dict]) -> str:
    msg = chat_view(thread)[-1]
    return env.get_template("partials/chat_message.html").render(msg=msg)


async def _answer(user: User, item_id: uuid.UUID, question: str, web: bool, queue: asyncio.Queue) -> None:
    try:
        async for event in chat.respond(user, item_id, question, web=web):
            if event.kind == "done":
                await queue.put(("done", {"html": _answer_html(event.data["thread"])}))
            else:
                await queue.put((event.kind, {"text": event.data}))
    except Exception:
        log.exception("article chat crashed for %s", item_id)
        await queue.put(("error", {"text": "Something went wrong while answering. Try again."}))
    finally:
        await queue.put(None)


async def _relay(queue: asyncio.Queue) -> AsyncIterator[str]:
    yield ": open\n\n"  # headers and a first byte now, so nothing in between waits for the model
    while True:
        try:
            entry = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
        except TimeoutError:
            yield ": keep-alive\n\n"
            continue
        if entry is None:
            return
        yield sse(*entry)


def _one_event(kind: str, text: str) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        yield sse(kind, {"text": text})

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/items/{item_id}/chat")
async def ask(
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    question: Annotated[str, Form()] = "",
    web: Annotated[str, Form()] = "",
):
    item, _feed = await load_item(session, user, item_id)
    question = question.strip()[: chat.QUESTION_MAX_CHARS]
    use_web = web in {"1", "on", "true"} and websearch.available()
    key = (user.id, item.id)
    if not ai_on(user, "chat"):
        return _one_event("error", "Chat is turned off. Turn it on in Manage → AI and memory.")
    if not question:
        return _one_event("error", "Ask a question first.")
    if key in _RUNNING:
        return _one_event("error", "Still answering your last question about this article.")
    if websearch.available() and web_default(user) != use_web:
        db_user = await session.get(User, user.id)
        db_user.settings = {**(db_user.settings or {}), "chat_web": use_web}
        await session.commit()
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_answer(user, item.id, question, use_web, queue))
    _RUNNING[key] = task
    task.add_done_callback(lambda _t: _RUNNING.pop(key, None))
    return StreamingResponse(_relay(queue), media_type="text/event-stream", headers=SSE_HEADERS)


async def chatted_items(session: AsyncSession, user: User, item_ids: list[str]) -> set[str]:
    """Which of these items (ids as strings) already have a conversation."""
    ids = []
    for raw in item_ids:
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    if not ids:
        return set()
    rows = await session.scalars(
        select(ItemChat.item_id).where(ItemChat.user_id == user.id, ItemChat.item_id.in_(ids))
    )
    return {str(i) for i in rows}


__all__ = ["chat_view", "chatted_items", "router", "sse"]
