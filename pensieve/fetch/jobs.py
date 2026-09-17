"""arq job functions for fetching. Each job opens its own session via ``session_scope``."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from arq import cron

from pensieve.db import session_scope
from pensieve.fetch import scheduler
from pensieve.fetch.reader_mode import extract_reader_html
from pensieve.fetch.subscribe import refresh_feed
from pensieve.models import Feed, Item

log = logging.getLogger(__name__)


async def fetch_feed(ctx: dict, feed_id: str) -> int:
    """Poll one feed. Returns the number of new items."""
    async with session_scope() as session:
        feed = await session.get(Feed, uuid.UUID(feed_id))
        if feed is None:
            log.info("fetch_feed: feed %s no longer exists", feed_id)
            return 0
        items = await refresh_feed(session, feed)
        return len(items)


async def fetch_reader_mode(ctx: dict, item_id: str) -> bool:
    """Fill ``item.reader_html`` from the linked page. Returns whether extraction succeeded."""
    async with session_scope() as session:
        item = await session.get(Item, uuid.UUID(item_id))
        if item is None or not item.url:
            return False
        html = await extract_reader_html(item.url)
        item.reader_fetched_at = datetime.now(UTC)
        if html:
            item.reader_html = html
        return html is not None


async def fetch_due_feeds(ctx: dict) -> int:
    """Cron: enqueue ``fetch_feed`` for every feed whose ``next_fetch_at`` has passed."""
    async with session_scope() as session:
        return await scheduler.enqueue_due_feeds(session)


async def prune_old_items(ctx: dict) -> int:
    """Cron: apply the retention policy."""
    async with session_scope() as session:
        return await scheduler.prune_old_items(session)


async def refresh_favicons(ctx: dict) -> int:
    """Cron (weekly): cache favicon bytes for feeds missing one or not refreshed in ``favicon_refresh_days``."""
    from pensieve.fetch.favicon import refresh_stale_icons

    async with session_scope() as session:
        return await refresh_stale_icons(session)


FUNCTIONS = [fetch_feed, fetch_reader_mode, fetch_due_feeds, prune_old_items, refresh_favicons]
CRON_JOBS = [
    cron(fetch_due_feeds, minute=set(range(60)), unique=True),
    cron(prune_old_items, hour=3, minute=15, unique=True),
    cron(refresh_favicons, weekday=0, hour=4, minute=5, unique=True),
]

__all__ = [
    "CRON_JOBS",
    "FUNCTIONS",
    "fetch_due_feeds",
    "fetch_feed",
    "fetch_reader_mode",
    "prune_old_items",
    "refresh_favicons",
]
