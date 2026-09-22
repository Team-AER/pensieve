"""Scheduling logic used by the arq jobs: which feeds are due, and retention pruning."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import any_, delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.config import get_settings
from pensieve.models import FEED_KIND_SAVED, Cluster, Feed, Insight, Item, ItemState, Note

log = logging.getLogger(__name__)

CLAIM_BATCH = 200
CLAIM_HOLD = timedelta(minutes=5)


async def due_feed_ids(session: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
    now = now or datetime.now(UTC)
    stmt = (
        select(Feed.id)
        .where(Feed.paused.is_(False), Feed.next_fetch_at.is_not(None), Feed.next_fetch_at <= now)
        .order_by(Feed.next_fetch_at)
    )
    return list((await session.scalars(stmt)).all())


async def claim_due_feeds(
    session: AsyncSession, now: datetime | None = None, limit: int = CLAIM_BATCH
) -> list[uuid.UUID]:
    """Atomically claim up to ``limit`` due feeds by pushing ``next_fetch_at`` forward ``CLAIM_HOLD``.

    One statement (``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING id``) so
    concurrent schedulers never enqueue the same feed twice; ``refresh_feed`` re-plans ``next_fetch_at``.
    """
    now = now or datetime.now(UTC)
    due = (
        select(Feed.id)
        .where(Feed.paused.is_(False), Feed.next_fetch_at.is_not(None), Feed.next_fetch_at <= now)
        .order_by(Feed.next_fetch_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = update(Feed).where(Feed.id.in_(due)).values(next_fetch_at=now + CLAIM_HOLD).returning(Feed.id)
    ids = list((await session.scalars(stmt)).all())
    await session.commit()
    return ids


async def enqueue_due_feeds(session: AsyncSession, now: datetime | None = None) -> int:
    """Claim due feeds in one statement, then enqueue one ``fetch_feed`` job per claimed id."""
    count = 0
    for feed_id in await claim_due_feeds(session, now):
        try:
            await queue.enqueue(queue.FETCH_FEED, str(feed_id), _job_id=queue.job_id_for("fetch", feed_id))
            count += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enqueue fetch for feed %s: %s", feed_id, exc)
            break
    return count


def _prunable_items(cutoff: datetime):
    starred = exists().where(ItemState.item_id == Item.id, ItemState.is_starred.is_(True))
    noted = exists().where(Note.item_id == Item.id)
    canonical = exists().where(Cluster.canonical_item_id == Item.id)
    in_insight = exists().where(Item.id == any_(Insight.item_refs))
    saved = exists().where(Feed.id == Item.feed_id, Feed.kind == FEED_KIND_SAVED)  # saved links are kept for good
    return Item.published_at < cutoff, ~starred, ~noted, ~canonical, ~in_insight, ~saved


async def prune_old_items(session: AsyncSession, now: datetime | None = None) -> int:
    """Delete items older than ``item_retention_days`` unless saved, starred, noted, canonical or cited by an insight."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=get_settings().item_retention_days)
    result = await session.execute(delete(Item).where(*_prunable_items(cutoff)))
    deleted = result.rowcount or 0
    if deleted:
        log.info("pruned %d items older than %s", deleted, cutoff.date())
    return deleted


__all__ = ["claim_due_feeds", "due_feed_ids", "enqueue_due_feeds", "prune_old_items"]
