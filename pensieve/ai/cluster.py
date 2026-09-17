"""Story clustering: exact duplicates, series, and cross-source stories by embedding similarity.

Order per new item: exact hash dedup -> series detection -> embedding neighbours (or title Jaccard when
embeddings are unavailable) -> override check -> LLM confirmation for borderline pairs -> create/extend cluster.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import prompts
from pensieve.ai.client import LLMClient, LLMError, get_client
from pensieve.ai.common import ai_on, user_feed_ids
from pensieve.ai.embeddings import get_vectors, nearest
from pensieve.config import get_settings

log = logging.getLogger(__name__)

SERIES_RE = re.compile(r"\bPart\s+\d+\b|\(\d+/\d+\)|#\d+", re.IGNORECASE)
SERIES_WINDOW_DAYS = 45
JACCARD_THRESHOLD = 0.6
BORDERLINE_MARGIN = 0.06
NEIGHBOUR_LIMIT = 15
JACCARD_POOL = 400
CONFIRM_TEXT_CHARS = 600
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "at",
    "by",
    "from",
    "as",
    "its",
    "it",
    "this",
    "that",
    "new",
    "how",
    "why",
    "what",
    "your",
    "you",
    "into",
    "vs",
}


def title_tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9+.-]*", title.lower()) if len(t) > 2 and t not in _STOP}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def series_key(title: str) -> str:
    return re.sub(r"\s+", " ", SERIES_RE.sub(" ", title)).strip(" :-–—,").lower()


def _pair(a: uuid.UUID, b: uuid.UUID) -> frozenset:
    return frozenset((a, b))


async def load_overrides(session: AsyncSession, user_id: uuid.UUID) -> dict[frozenset, str]:
    rows = (
        await session.scalars(select(models.ClusterOverride).where(models.ClusterOverride.user_id == user_id))
    ).all()
    out: dict[frozenset, str] = {}
    for row in rows:
        key = _pair(row.item_a, row.item_b)
        # a later split beats an earlier merge and vice versa; rows come in insertion order (uuid pk, so sort)
        out[key] = row.action
    return out


async def cluster_of(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID) -> models.Cluster | None:
    stmt = (
        select(models.Cluster)
        .join(models.ClusterItem, models.ClusterItem.cluster_id == models.Cluster.id)
        .where(models.Cluster.user_id == user_id, models.ClusterItem.item_id == item_id)
    )
    return await session.scalar(stmt)


async def cluster_members(session: AsyncSession, cluster_id: uuid.UUID) -> list[models.Item]:
    stmt = (
        select(models.Item)
        .join(models.ClusterItem, models.ClusterItem.item_id == models.Item.id)
        .where(models.ClusterItem.cluster_id == cluster_id)
        .order_by(models.Item.published_at)
    )
    return list((await session.scalars(stmt)).all())


async def _recompute(session: AsyncSession, cluster: models.Cluster, headline: str | None = None) -> None:
    members = await cluster_members(session, cluster.id)
    if not members:
        return
    earliest = members[0]
    cluster.canonical_item_id = earliest.id
    cluster.window_start = min(m.published_at for m in members)
    cluster.window_end = max(m.published_at for m in members)
    cluster.source_count = len({m.feed_id for m in members})
    if headline:
        cluster.headline = headline[:500]
    elif not cluster.headline:
        cluster.headline = earliest.title[:500]


async def _add_member(session: AsyncSession, cluster: models.Cluster, item: models.Item, sim: float) -> None:
    existing = await session.scalar(
        select(models.ClusterItem).where(
            models.ClusterItem.cluster_id == cluster.id, models.ClusterItem.item_id == item.id
        )
    )
    if existing is None:
        session.add(models.ClusterItem(cluster_id=cluster.id, item_id=item.id, similarity=round(sim, 4)))
        await session.flush()


async def _new_cluster(
    session: AsyncSession,
    user: models.User,
    kind: str,
    members: list[tuple[models.Item, float]],
    headline: str | None,
) -> models.Cluster:
    earliest = min((m for m, _ in members), key=lambda m: m.published_at)
    cluster = models.Cluster(
        user_id=user.id,
        kind=kind,
        headline=(headline or earliest.title)[:500],
        window_start=earliest.published_at,
        window_end=earliest.published_at,
        canonical_item_id=earliest.id,
        source_count=1,
    )
    session.add(cluster)
    await session.flush()
    for item, sim in members:
        await _add_member(session, cluster, item, sim)
    await _recompute(session, cluster, headline)
    return cluster


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


async def _exact_duplicates(
    session: AsyncSession, user: models.User, item: models.Item, hours: int
) -> list[models.Item]:
    lo, hi = item.published_at - timedelta(hours=hours), item.published_at + timedelta(hours=hours)
    stmt = select(models.Item).where(
        models.Item.feed_id.in_(user_feed_ids(user.id)),
        models.Item.hash == item.hash,
        models.Item.id != item.id,
        models.Item.published_at.between(lo, hi),
    )
    return list((await session.scalars(stmt)).all())


async def _series_siblings(session: AsyncSession, item: models.Item) -> list[models.Item]:
    if not SERIES_RE.search(item.title or ""):
        return []
    key = series_key(item.title)
    if len(key) < 4:
        return []
    lo = item.published_at - timedelta(days=SERIES_WINDOW_DAYS)
    hi = item.published_at + timedelta(days=SERIES_WINDOW_DAYS)
    stmt = select(models.Item).where(
        models.Item.feed_id == item.feed_id,
        models.Item.id != item.id,
        models.Item.published_at.between(lo, hi),
    )
    rows = (await session.scalars(stmt)).all()
    return [r for r in rows if SERIES_RE.search(r.title or "") and series_key(r.title) == key]


async def _story_candidates(
    session: AsyncSession, user: models.User, item: models.Item, hours: int
) -> list[tuple[models.Item, float]]:
    """(item, similarity) pairs within the window, best first. Embeddings first; Jaccard on titles otherwise."""
    lo, hi = item.published_at - timedelta(hours=hours), item.published_at + timedelta(hours=hours)
    vec = (await get_vectors(session, [item.id])).get(item.id)
    if vec is not None:
        rows = await nearest(session, user, vec, limit=NEIGHBOUR_LIMIT, since=lo, exclude_item_ids={item.id})
        return [(other, sim) for other, sim in rows if other.published_at <= hi]
    stmt = (
        select(models.Item)
        .where(
            models.Item.feed_id.in_(user_feed_ids(user.id)),
            models.Item.id != item.id,
            models.Item.published_at.between(lo, hi),
        )
        .order_by(models.Item.published_at.desc())
        .limit(JACCARD_POOL)
    )
    mine = title_tokens(item.title)
    scored = []
    for other in (await session.scalars(stmt)).all():
        j = jaccard(mine, title_tokens(other.title))
        if j >= JACCARD_THRESHOLD:
            scored.append((other, j))
    scored.sort(key=lambda p: -p[1])
    return scored[:NEIGHBOUR_LIMIT]


async def _confirm(
    client: LLMClient, item: models.Item, other: models.Item, feeds: dict[uuid.UUID, str]
) -> tuple[bool, str | None]:
    try:
        result = await client.chat_json(
            get_settings().llm_fast_model,
            prompts.CLUSTER_CONFIRM_SYSTEM,
            prompts.cluster_confirm_user(
                item.title,
                (item.content_text or "")[:CONFIRM_TEXT_CHARS],
                feeds.get(item.feed_id, ""),
                other.title,
                (other.content_text or "")[:CONFIRM_TEXT_CHARS],
                feeds.get(other.feed_id, ""),
            ),
            prompts.CLUSTER_CONFIRM_SCHEMA,
            max_tokens=120,
            workflow="cluster",
            name="cluster_confirm",
        )
    except LLMError as exc:
        log.warning("cluster confirmation failed: %s", exc)
        return False, None
    return bool(result["same_story"]), (result.get("headline") or None)


async def _headline(client: LLMClient, item: models.Item, other: models.Item, feeds: dict) -> str | None:
    same, headline = await _confirm(client, item, other, feeds)
    return headline if same else None


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


async def cluster_items(
    session: AsyncSession, user: models.User, items: list[models.Item], client: LLMClient | None = None
) -> list[models.Cluster]:
    """Assign each of ``items`` to at most one cluster. Returns clusters touched."""
    if not items or not ai_on(user, "group_stories"):
        return []
    client = client or get_client()
    settings = get_settings()
    hours = settings.cluster_window_hours
    threshold = settings.cluster_similarity_threshold
    overrides = await load_overrides(session, user.id)
    feeds = {
        f.id: f.title
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    touched: dict[uuid.UUID, models.Cluster] = {}

    for item in sorted(items, key=lambda i: i.published_at):
        if await cluster_of(session, user.id, item.id) is not None:
            continue

        def allowed(other: models.Item, _item=item) -> bool:
            return overrides.get(_pair(_item.id, other.id)) != "split"

        # 1. exact duplicates across feeds
        dupes = [d for d in await _exact_duplicates(session, user, item, hours) if allowed(d)]
        if dupes:
            cluster = None
            for d in dupes:
                cluster = await cluster_of(session, user.id, d.id)
                if cluster is not None:
                    break
            if cluster is None:
                cluster = await _new_cluster(
                    session, user, "duplicate", [(item, 1.0), *[(d, 1.0) for d in dupes]], None
                )
            else:
                await _add_member(session, cluster, item, 1.0)
                await _recompute(session, cluster)
            touched[cluster.id] = cluster
            continue

        # 2. series within one feed
        siblings = [s for s in await _series_siblings(session, item) if allowed(s)]
        if siblings:
            cluster = None
            for s in siblings:
                cluster = await cluster_of(session, user.id, s.id)
                if cluster is not None and cluster.kind == "series":
                    break
                cluster = None
            if cluster is None:
                free = [s for s in siblings if await cluster_of(session, user.id, s.id) is None]
                cluster = await _new_cluster(
                    session,
                    user,
                    "series",
                    [(item, 1.0), *[(s, 1.0) for s in free]],
                    series_key(item.title).title(),
                )
            else:
                await _add_member(session, cluster, item, 1.0)
                await _recompute(session, cluster)
            touched[cluster.id] = cluster
            continue

        # 3. cross-source story
        candidates = await _story_candidates(session, user, item, hours)
        forced = {
            other
            for key, action in overrides.items()
            if action == "merge" and item.id in key
            for other in key
            if other != item.id
        }
        if forced:
            known = {c.id for c, _ in candidates}
            missing = [oid for oid in forced if oid not in known]
            if missing:
                for other in (
                    await session.scalars(select(models.Item).where(models.Item.id.in_(missing)))
                ).all():
                    candidates.append((other, 1.0))
            candidates = [(c, 1.0 if c.id in forced else s) for c, s in candidates]
            candidates.sort(key=lambda p: -p[1])

        for other, sim in candidates:
            if not allowed(other):
                continue
            headline: str | None = None
            if other.id in forced or sim >= threshold + BORDERLINE_MARGIN:
                merge = True
            elif sim >= threshold:
                merge, headline = await _confirm(client, item, other, feeds)
            else:
                merge = False
            if not merge:
                continue
            cluster = await cluster_of(session, user.id, other.id)
            if cluster is None:
                if headline is None:
                    headline = await _headline(client, item, other, feeds)
                cluster = await _new_cluster(session, user, "story", [(other, 1.0), (item, sim)], headline)
            else:
                if cluster.kind == "duplicate":
                    cluster.kind = "story"
                await _add_member(session, cluster, item, sim)
                await _recompute(session, cluster)
            touched[cluster.id] = cluster
            break

    await session.flush()
    return list(touched.values())


async def unmerge(session: AsyncSession, user: models.User, item_id: uuid.UUID) -> None:
    """Split ``item_id`` out of its cluster and record split overrides against every other member."""
    cluster = await cluster_of(session, user.id, item_id)
    if cluster is None:
        return
    members = await cluster_members(session, cluster.id)
    others = [m for m in members if m.id != item_id]
    await session.execute(
        delete(models.ClusterItem).where(
            models.ClusterItem.cluster_id == cluster.id, models.ClusterItem.item_id == item_id
        )
    )
    existing = await load_overrides(session, user.id)
    for other in others:
        key = _pair(item_id, other.id)
        if existing.get(key) == "split":
            continue
        if existing.get(key) == "merge":
            await session.execute(
                delete(models.ClusterOverride).where(
                    models.ClusterOverride.user_id == user.id,
                    models.ClusterOverride.item_a.in_([item_id, other.id]),
                    models.ClusterOverride.item_b.in_([item_id, other.id]),
                )
            )
        session.add(models.ClusterOverride(user_id=user.id, item_a=item_id, item_b=other.id, action="split"))
    session.add(
        models.Correction(
            user_id=user.id,
            target_type="cluster",
            target_id=item_id,
            field="cluster",
            old_value=str(cluster.id),
            new_value=None,
        )
    )
    if len(others) <= 1:
        await session.delete(cluster)
    else:
        await _recompute(session, cluster)
    await session.flush()
