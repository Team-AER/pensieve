"""Reader memory: versioned profile, related history, ask-my-reading, corrections.

Skip detection (documented approximation): an item is *skipped* when it was marked read inside a mark-all batch
(>= 5 item_states of the user sharing the same read_at second, see ``common.MARK_ALL_BATCH_MIN``); an item is
*opened* when it was marked read outside such a batch. Stars count as strong positive signal either way.
"""

from __future__ import annotations

import difflib
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import prompts
from pensieve.ai.client import LLMClient, get_client
from pensieve.ai.common import (
    ai_on,
    is_batch_read,
    mark_all_batch_times,
    read_states,
    user_feed_ids,
    utcnow,
    window,
)
from pensieve.ai.embeddings import embed_items, get_vectors, nearest
from pensieve.config import get_settings

PROFILE_DAYS = 90
PROFILE_MAX_LINES = 40
ASK_TOP_K = 20
ASK_EXCERPT_CHARS = 1200
ASK_INSIGHT_KIND = "ask"
NO_HISTORY_PREAMBLE = (
    "You have not read or starred anything yet, so this answer draws on everything in your subscriptions.\n\n"
)
MEMORY_OFF_TEXT = "Reader memory is switched off in your AI settings, so there is nothing to ask."


@dataclass
class RelatedItem:
    item: models.Item
    note: models.Note | None
    similarity: float


@dataclass
class Answer:
    text: str
    citations: list[models.Item] = field(default_factory=list)
    from_history: bool = True
    """False when the user has no read/starred history and the whole subscription pool was searched."""


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


async def latest_profile(session: AsyncSession, user_id: uuid.UUID) -> models.Profile | None:
    stmt = (
        select(models.Profile)
        .where(models.Profile.user_id == user_id)
        .order_by(models.Profile.version.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def profile_text(session: AsyncSession, user_id: uuid.UUID) -> str:
    p = await latest_profile(session, user_id)
    return p.body_text if p else ""


async def gather_signals(session: AsyncSession, user: models.User, days: int = PROFILE_DAYS) -> dict:
    """Counts of what the reader opened, skipped, starred, corrected and annotated in the last ``days``."""
    since = window(days=days)
    batches = await mark_all_batch_times(session, user.id, since)
    states = await read_states(session, user.id, since)
    starred = list(
        (
            await session.scalars(
                select(models.ItemState).where(
                    models.ItemState.user_id == user.id,
                    models.ItemState.is_starred.is_(True),
                )
            )
        ).all()
    )
    ids = {s.item_id for s in states} | {s.item_id for s in starred}
    items: dict[uuid.UUID, models.Item] = {}
    if ids:
        rows = (await session.scalars(select(models.Item).where(models.Item.id.in_(list(ids))))).all()
        items = {i.id: i for i in rows}
    feeds = {
        f.id: f.title
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    ai_rows = {}
    if ids:
        ai_rows = {
            r.item_id: r
            for r in (
                await session.scalars(
                    select(models.ItemAI).where(
                        models.ItemAI.user_id == user.id, models.ItemAI.item_id.in_(list(ids))
                    )
                )
            ).all()
        }

    opened_by_tag: Counter = Counter()
    opened_by_feed: Counter = Counter()
    skipped_by_feed: Counter = Counter()
    skipped_by_tag: Counter = Counter()
    opened_titles: list[str] = []
    for s in sorted(states, key=lambda s: s.read_at or since, reverse=True):
        item = items.get(s.item_id)
        if item is None:
            continue
        tags = ai_rows[item.id].tags if item.id in ai_rows else []
        if is_batch_read(s, batches):
            skipped_by_feed[feeds.get(item.feed_id, "?")] += 1
            skipped_by_tag.update(tags)
        else:
            opened_by_feed[feeds.get(item.feed_id, "?")] += 1
            opened_by_tag.update(tags)
            if len(opened_titles) < 30:
                opened_titles.append(item.title)
    starred_titles = [
        items[s.item_id].title
        for s in sorted(starred, key=lambda s: s.starred_at or since, reverse=True)
        if s.item_id in items
    ][:20]
    corrections = (
        await session.scalars(
            select(models.Correction)
            .where(models.Correction.user_id == user.id, models.Correction.created_at >= since)
            .order_by(models.Correction.created_at.desc())
            .limit(30)
        )
    ).all()
    notes = (
        await session.scalars(
            select(models.Note)
            .where(models.Note.user_id == user.id, models.Note.created_at >= since)
            .order_by(models.Note.created_at.desc())
            .limit(20)
        )
    ).all()
    return {
        "days": days,
        "opened_count": sum(opened_by_feed.values()),
        "skipped_count": sum(skipped_by_feed.values()),
        "opened_by_tag": dict(opened_by_tag.most_common(20)),
        "skipped_by_tag": dict(skipped_by_tag.most_common(10)),
        "opened_by_feed": dict(opened_by_feed.most_common(15)),
        "skipped_by_feed": dict(skipped_by_feed.most_common(10)),
        "recent_opened_titles": opened_titles,
        "starred_titles": starred_titles,
        "corrections": [
            {"type": c.target_type, "field": c.field, "from": c.old_value, "to": c.new_value}
            for c in corrections
        ],
        "notes": [{"quote": n.quote[:200], "body": n.body[:300]} for n in notes],
    }


def _clip_lines(text: str, max_lines: int = PROFILE_MAX_LINES) -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines])


async def refresh_profile(
    session: AsyncSession, user: models.User, client: LLMClient | None = None
) -> models.Profile | None:
    """Regenerate the profile from 90 days of signals; stores a new version with a unified diff."""
    if not ai_on(user, "memory"):
        return None
    client = client or get_client()
    previous = await latest_profile(session, user.id)
    prev_text = previous.body_text if previous else ""
    signals = await gather_signals(session, user)
    result = await client.chat_json(
        get_settings().llm_long_model,
        prompts.PROFILE_SYSTEM,
        prompts.profile_user(prev_text, signals),
        prompts.PROFILE_SCHEMA,
        max_tokens=1200,
        workflow="profile",
        name="profile",
        reasoning=get_settings().llm_digest_reasoning,
    )
    body = _clip_lines(str(result["profile_text"]))
    diff = "".join(
        difflib.unified_diff(
            prev_text.splitlines(keepends=True),
            (body + "\n").splitlines(keepends=True),
            fromfile=f"profile v{previous.version}" if previous else "profile v0",
            tofile=f"profile v{(previous.version + 1) if previous else 1}",
        )
    )
    row = models.Profile(
        user_id=user.id,
        version=(previous.version + 1) if previous else 1,
        body_text=body,
        diff_from_previous=diff or None,
        edited_by_user=False,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Related history
# ---------------------------------------------------------------------------


async def _history_pool(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    stmt = select(models.ItemState.item_id).where(
        models.ItemState.user_id == user_id,
        (models.ItemState.is_read.is_(True)) | (models.ItemState.is_starred.is_(True)),
    )
    return list((await session.scalars(stmt)).all())


async def _same_cluster_ids(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID) -> set[uuid.UUID]:
    mine = (
        select(models.ClusterItem.cluster_id)
        .join(models.Cluster)
        .where(models.Cluster.user_id == user_id, models.ClusterItem.item_id == item_id)
    )
    stmt = select(models.ClusterItem.item_id).where(models.ClusterItem.cluster_id.in_(mine))
    return set((await session.scalars(stmt)).all())


async def related_history(
    session: AsyncSession,
    user: models.User,
    item: models.Item,
    limit: int = 5,
    client: LLMClient | None = None,
) -> list[RelatedItem]:
    if not ai_on(user, "memory"):
        return []
    client = client or get_client()
    vec = (await get_vectors(session, [item.id])).get(item.id)
    if vec is None:
        if await embed_items(session, [item], client, workflow="related_history") is None:
            return []
        vec = (await get_vectors(session, [item.id])).get(item.id)
        if vec is None:
            return []
    pool = set(await _history_pool(session, user.id))
    pool -= await _same_cluster_ids(session, user.id, item.id)
    pool.discard(item.id)
    if not pool:
        return []
    rows = await nearest(session, user, vec, limit=limit, only_item_ids=list(pool))
    notes = {}
    if rows:
        for n in (
            await session.scalars(
                select(models.Note)
                .where(models.Note.user_id == user.id, models.Note.item_id.in_([r.id for r, _ in rows]))
                .order_by(models.Note.created_at.desc())
            )
        ).all():
            notes.setdefault(n.item_id, n)
    return [
        RelatedItem(item=other, note=notes.get(other.id), similarity=round(sim, 4)) for other, sim in rows
    ]


# ---------------------------------------------------------------------------
# Ask my reading
# ---------------------------------------------------------------------------


_STOPWORDS = frozenset(
    [
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "has",
        "have",
        "had",
        "did",
        "does",
        "do",
        "about",
        "the",
        "and",
        "for",
        "from",
        "with",
        "this",
        "that",
        "these",
        "those",
        "into",
        "over",
        "under",
        "after",
        "before",
        "last",
        "month",
        "week",
        "year",
        "today",
        "yesterday",
        "recently",
        "changed",
        "change",
        "read",
        "reading",
        "anything",
        "something",
        "things",
        "there",
        "their",
        "they",
        "them",
        "was",
        "were",
        "been",
        "being",
        "are",
        "you",
        "your",
        "mine",
    ]
)


async def _fulltext(
    session: AsyncSession,
    user_id: uuid.UUID,
    question: str,
    limit: int,
    only_item_ids: list[uuid.UUID] | None = None,
) -> list[models.Item]:
    """Full-text candidates, restricted to ``only_item_ids`` (the read/starred pool) when given."""

    async def run(query) -> list[models.Item]:
        stmt = (
            select(models.Item)
            .where(models.Item.feed_id.in_(user_feed_ids(user_id)), models.Item.search_vector.op("@@")(query))
            .order_by(func.ts_rank(models.Item.search_vector, query).desc())
            .limit(limit)
        )
        if only_item_ids is not None:
            stmt = stmt.where(models.Item.id.in_(only_item_ids))
        return list((await session.scalars(stmt)).all())

    # Strict pass: every term must match (websearch semantics). Questions rarely reuse the article's
    # exact words, so when that yields little, fall back to ANY significant term, ranked by ts_rank.
    hits = await run(func.websearch_to_tsquery("english", question))
    if len(hits) >= max(3, limit // 4):
        return hits
    terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", question) if t.lower() not in _STOPWORDS]
    if not terms:
        return hits
    loose = await run(
        func.websearch_to_tsquery("english", " OR ".join(re.sub(r"[^A-Za-z0-9-]", "", t) for t in terms))
    )
    seen = {i.id for i in hits}
    return hits + [i for i in loose if i.id not in seen][: max(0, limit - len(hits))]


async def ask_reading(
    session: AsyncSession, user: models.User, question: str, client: LLMClient | None = None
) -> Answer:
    """Answer from the read/starred pool; only a user with no history at all is answered from every
    subscribed item (and told so). Each Q&A is kept as an ``insights`` row of kind ``ask``."""
    if not ai_on(user, "memory"):
        return Answer(text=MEMORY_OFF_TEXT, citations=[])
    client = client or get_client()
    settings = get_settings()
    pool = await _history_pool(session, user.id)
    from_history = bool(pool)
    candidates: list[models.Item] = []
    qvec = await client.embed([question], workflow="ask")
    if qvec:
        rows = await nearest(session, user, qvec[0], limit=ASK_TOP_K, only_item_ids=pool or None)
        candidates.extend(i for i, _ in rows)
    candidates.extend(await _fulltext(session, user.id, question, ASK_TOP_K, only_item_ids=pool or None))
    seen: set[uuid.UUID] = set()
    unique: list[models.Item] = []
    for it in candidates:
        if it.id not in seen:
            seen.add(it.id)
            unique.append(it)
    if not unique:
        answer = Answer(
            text="I could not find anything in your reading that answers that.",
            citations=[],
            from_history=from_history,
        )
        await _store_ask(session, user.id, question, answer)
        return answer

    feeds = {
        f.id: f.title
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    budget_chars = (settings.llm_max_input_tokens_long - 1500) * 4
    excerpts: list[tuple[int, str, str, str]] = []
    used = 0
    for n, it in enumerate(unique, start=1):
        text = (it.content_text or "")[:ASK_EXCERPT_CHARS]
        cost = len(text) + len(it.title) + 40
        if used + cost > budget_chars:
            break
        excerpts.append((n, it.title, feeds.get(it.feed_id, ""), text))
        used += cost
    result = await client.chat_json(
        settings.llm_long_model,
        prompts.ASK_SYSTEM,
        prompts.ask_user(question, excerpts),
        prompts.ASK_ANSWER_SCHEMA,
        max_tokens=800,
        workflow="ask",
        name="ask_answer",
    )
    text = str(result["answer"])
    cited = {int(n) for n in result.get("citations", []) if isinstance(n, int)}
    cited |= {int(m) for m in re.findall(r"\[(\d+)\]", text)}
    by_n = {n: unique[n - 1] for n, *_ in excerpts}
    citations = [by_n[n] for n in sorted(cited) if n in by_n]
    if not from_history:
        text = NO_HISTORY_PREAMBLE + text
    answer = Answer(text=text, citations=citations, from_history=from_history)
    await _store_ask(session, user.id, question, answer)
    return answer


async def _store_ask(session: AsyncSession, user_id: uuid.UUID, question: str, answer: Answer) -> None:
    """Persist one Q&A as an ``insights`` row (kind ``ask``; period = UTC timestamp, unique per user)."""
    session.add(
        models.Insight(
            user_id=user_id,
            kind=ASK_INSIGHT_KIND,
            period=utcnow().strftime("%Y%m%d%H%M%S%f"),
            title=question[:300],
            body={
                "question": question,
                "answer": answer.text,
                "citations": [str(i.id) for i in answer.citations],
                "from_history": answer.from_history,
            },
            item_refs=[i.id for i in answer.citations],
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


async def record_correction(
    session: AsyncSession,
    user: models.User,
    target_type: str,
    target_id: uuid.UUID | str,
    field_name: str,
    old: str | None,
    new: str | None,
) -> models.Correction:
    row = models.Correction(
        user_id=user.id,
        target_type=target_type,
        target_id=uuid.UUID(str(target_id)),
        field=field_name,
        old_value=old,
        new_value=new,
    )
    session.add(row)
    await session.flush()
    return row
