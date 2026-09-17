"""Scheduling logic used by the arq jobs: which feeds are due, and retention pruning."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import any_, delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.config import get_settings
from pensieve.models import Cluster, Feed, Insight, Item, ItemState, Note

log = logging.getLogger(__name__)


async def due_feed_ids(session: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
    now = now or datetime.now(UTC)
    stmt = (
        select(Feed.id)
        .where(Feed.paused.is_(False), Feed.next_fetch_at.is_not(None), Feed.next_fetch_at <= now)
        .order_by(Feed.next_fetch_at)
    )
    return list((await session.scalars(stmt)).all())


async def enqueue_due_feeds(session: AsyncSession, now: datetime | None = None) -> int:
    """Enqueue one ``fetch_feed`` job per due feed; deterministic job ids collapse duplicates."""
    count = 0
    for feed_id in await due_feed_ids(session, now):
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
    return Item.published_at < cutoff, ~starred, ~noted, ~canonical, ~in_insight


async def prune_old_items(session: AsyncSession, now: datetime | None = None) -> int:
    """Delete items older than ``item_retention_days`` unless starred, noted, canonical or cited by an insight."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=get_settings().item_retention_days)
    result = await session.execute(delete(Item).where(*_prunable_items(cutoff)))
    deleted = result.rowcount or 0
    if deleted:
        log.info("pruned %d items older than %s", deleted, cutoff.date())
    return deleted


__all__ = ["due_feed_ids", "enqueue_due_feeds", "prune_old_items"]
