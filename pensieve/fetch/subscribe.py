"""Subscribing to feeds and polling them: conditional GET, item insertion, rules, adaptive scheduling."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.config import get_settings
from pensieve.fetch import http as fetch_http
from pensieve.fetch.discover import Discovered, DiscoveryError, discover
from pensieve.fetch.parse import ParsedEntry, ParsedFeed, ParseError, parse_feed
from pensieve.models import Feed, FeedRule, Item, ItemState, User

log = logging.getLogger(__name__)

INTERVAL_FACTOR = 1.5
MAX_CONSECUTIVE_ERRORS = 10
PERMANENT_REDIRECTS = frozenset({301, 308})
RETRY_AFTER_STATUSES = frozenset({429, 503})
MAX_ERROR_TEXT = 1000
MAX_URL = 2048


class FeedError(Exception):
    """Subscribing failed: not a feed, unreachable, or already subscribed."""


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------------------------
# Adaptive interval and backoff
# --------------------------------------------------------------------------------------------------------------


def _clamp(minutes: float) -> int:
    settings = get_settings()
    return int(max(settings.fetch_min_interval_min, min(settings.fetch_max_interval_min, round(minutes))))


def shorten_interval(current: int) -> int:
    """New items appeared: poll sooner (divide by 1.5, clamp at the minimum)."""
    return _clamp(current / INTERVAL_FACTOR)


def lengthen_interval(current: int) -> int:
    """Nothing changed: poll later (multiply by 1.5, clamp at the maximum)."""
    return _clamp(max(current * INTERVAL_FACTOR, current + 1))


def backoff_interval(error_count: int) -> int:
    """Exponential backoff after ``error_count`` consecutive failures, starting from the default interval."""
    base = get_settings().fetch_default_interval_min
    return _clamp(base * (2 ** max(0, error_count - 1)))


def _schedule_success(feed: Feed, now: datetime, *, changed: bool) -> None:
    feed.error_count = 0
    feed.last_error = None
    feed.last_fetch_at = now
    feed.last_success_at = now
    current = feed.fetch_interval_min or get_settings().fetch_default_interval_min
    feed.fetch_interval_min = shorten_interval(current) if changed else lengthen_interval(current)
    feed.next_fetch_at = now + timedelta(minutes=feed.fetch_interval_min)


def _schedule_error(feed: Feed, now: datetime, error: str, *, retry_after: timedelta | None = None) -> None:
    feed.error_count = (feed.error_count or 0) + 1
    feed.last_error = error[:MAX_ERROR_TEXT]
    feed.last_fetch_at = now
    feed.fetch_interval_min = backoff_interval(feed.error_count)
    wait = timedelta(minutes=feed.fetch_interval_min)
    if retry_after is not None:
        # Honour the server's Retry-After (clamped to the max interval) instead of our own backoff.
        wait = max(timedelta(minutes=get_settings().fetch_min_interval_min), retry_after)
        wait = min(wait, timedelta(minutes=get_settings().fetch_max_interval_min))
        feed.fetch_interval_min = _clamp(wait.total_seconds() / 60)
    feed.next_fetch_at = now + wait
    if feed.error_count >= MAX_CONSECUTIVE_ERRORS:
        feed.paused = True
        log.warning("feed %s paused after %d consecutive errors: %s", feed.url, feed.error_count, error)
    else:
        log.info("feed %s error %d: %s", feed.url, feed.error_count, error)


def parse_retry_after(value: str | None, now: datetime) -> timedelta | None:
    """``Retry-After`` is either delay-seconds or an HTTP-date; ``None`` when absent or unparseable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(timedelta(0), when - now)


# --------------------------------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------------------------------


def _rule_matches(rule: FeedRule, item: Item) -> bool:
    field = rule.field or "title"
    if field == "body":
        haystack = item.content_text or ""
    elif field == "author":
        haystack = item.author or ""
    elif field == "url":
        haystack = item.url or ""
    else:
        haystack = item.title or ""
    if not rule.pattern:
        return False
    if rule.is_regex:
        try:
            return re.search(rule.pattern, haystack, re.IGNORECASE) is not None
        except re.error:
            log.warning("feed rule %s has invalid regex %r", rule.id, rule.pattern)
            return False
    return rule.pattern.lower() in haystack.lower()


async def load_rules(session: AsyncSession, user_id: uuid.UUID, feed_id: uuid.UUID) -> list[FeedRule]:
    stmt = select(FeedRule).where(
        FeedRule.user_id == user_id,
        FeedRule.enabled.is_(True),
        or_(FeedRule.feed_id == feed_id, FeedRule.feed_id.is_(None)),
    )
    return list((await session.scalars(stmt)).all())


def apply_rules(rules: list[FeedRule], user_id: uuid.UUID, items: list[Item], now: datetime) -> list[ItemState]:
    """Build ``ItemState`` rows for items matching any rule (hide/star/tag/mark_read). Not persisted here."""
    states: list[ItemState] = []
    for item in items:
        state: ItemState | None = None
        for rule in rules:
            if not _rule_matches(rule, item):
                continue
            if state is None:
                state = ItemState(user_id=user_id, item_id=item.id, tags=[])
            action = rule.action
            if action == "hide":
                state.hidden = True
            elif action == "star":
                state.is_starred = True
                state.starred_at = now
            elif action == "mark_read":
                state.is_read = True
                state.read_at = now
            elif action == "tag" and rule.action_value and rule.action_value not in state.tags:
                state.tags = [*state.tags, rule.action_value]
        if state is not None:
            states.append(state)
    return states


# --------------------------------------------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------------------------------------------


def _entry_to_item(feed: Feed, entry: ParsedEntry) -> Item:
    # No explicit id: Item.id defaults to a time-ordered UUIDv7 so the sync APIs' int64 ids follow insertion time.
    return Item(
        feed_id=feed.id,
        guid=entry.guid,
        url=entry.url,
        title=entry.title,
        author=entry.author,
        published_at=entry.published_at,
        content_html=entry.content_html,
        content_text=entry.content_text,
        hash=entry.hash,
        enclosure_url=entry.enclosure_url,
        enclosure_type=entry.enclosure_type,
    )


def _update_feed_metadata(feed: Feed, parsed: ParsedFeed) -> None:
    if parsed.title and not feed.title:
        feed.title = parsed.title[:300]
    if parsed.site_url and not feed.site_url:
        feed.site_url = parsed.site_url
    if parsed.description and not feed.description:
        feed.description = parsed.description
    if parsed.icon_url and not feed.icon_url:
        feed.icon_url = parsed.icon_url
    if parsed.hub and parsed.hub != feed.websub_hub:
        feed.websub_hub = parsed.hub


def _update_item_in_place(item: Item, entry: ParsedEntry, now: datetime) -> None:
    item.title = entry.title
    item.content_html = entry.content_html
    item.content_text = entry.content_text
    item.hash = entry.hash
    item.updated_at = now


async def ingest(session: AsyncSession, feed: Feed, parsed: ParsedFeed, *, now: datetime | None = None) -> list[Item]:
    """Insert entries not yet stored for this feed, apply rules, return the new ``Item`` rows.

    Dedupe order: by guid (a known guid whose content hash changed is updated in place, stamping
    ``updated_at``), then by content hash within the feed (a re-published article under a new guid is skipped).
    """
    now = now or _now()
    _update_feed_metadata(feed, parsed)

    fresh: dict[str, ParsedEntry] = {}
    for entry in parsed.entries:
        if entry.guid and entry.guid not in fresh:
            fresh[entry.guid] = entry
    if not fresh:
        return []

    existing_rows = (
        await session.scalars(select(Item).where(Item.feed_id == feed.id, Item.guid.in_(fresh)))
    ).all()
    existing = {row.guid: row for row in existing_rows}
    for guid, row in existing.items():
        entry = fresh[guid]
        if entry.hash and row.hash != entry.hash:
            _update_item_in_place(row, entry, now)

    candidates = [entry for guid, entry in fresh.items() if guid not in existing]
    if not candidates:
        await session.flush()
        return []
    hashes = {e.hash for e in candidates if e.hash}
    known_hashes = set(
        (await session.scalars(select(Item.hash).where(Item.feed_id == feed.id, Item.hash.in_(hashes)))).all()
    )
    items: list[Item] = []
    for entry in candidates:
        if entry.hash and entry.hash in known_hashes:
            continue
        known_hashes.add(entry.hash)
        items.append(_entry_to_item(feed, entry))
    if not items:
        await session.flush()
        return []

    session.add_all(items)
    rules = await load_rules(session, feed.user_id, feed.id)
    if rules:
        session.add_all(apply_rules(rules, feed.user_id, items, now))
    await session.flush()
    return items


async def _notify_new_items(feed: Feed, items: list[Item]) -> None:
    if not items:
        return
    try:
        # No _job_id: a fixed id would make arq drop a second fetch's item_ids while the first job is queued.
        await queue.enqueue(queue.AI_PROCESS_NEW_ITEMS, str(feed.id), [str(i.id) for i in items])
    except Exception as exc:  # noqa: BLE001 - a missing Redis must never fail a fetch
        log.warning("could not enqueue %s for feed %s: %s", queue.AI_PROCESS_NEW_ITEMS, feed.id, exc)


async def _cache_favicon(session: AsyncSession, feed: Feed) -> None:
    """Best-effort favicon download after a subscribe; never fails the caller."""
    try:
        from pensieve.fetch.favicon import refresh_feed_icon

        if await refresh_feed_icon(session, feed):
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.info("favicon for %s not cached: %s", feed.url, exc)


# --------------------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------------------


async def _feed_exists(session: AsyncSession, user_id: uuid.UUID, url: str) -> bool:
    stmt = select(Feed.id).where(Feed.user_id == user_id, Feed.url == url)
    return (await session.scalar(stmt)) is not None


async def add_feed(session: AsyncSession, user: User, url: str, folder_id: uuid.UUID | None = None) -> Feed:
    """Autodiscover a feed at ``url``, store it for ``user``, ingest its current entries.

    Raises :class:`FeedError` when nothing can be found, when the URL is unsafe, or when already subscribed.
    """
    url = (url or "").strip()
    if not url:
        raise FeedError("A URL is required.")
    if await _feed_exists(session, user.id, url):
        raise FeedError("You are already subscribed to this feed.")
    try:
        found: Discovered = await discover(url)
    except fetch_http.UnsafeURLError as exc:
        raise FeedError(f"That address cannot be fetched: {exc}") from exc
    except DiscoveryError as exc:
        raise FeedError(str(exc)) from exc
    if await _feed_exists(session, user.id, found.feed_url):
        raise FeedError("You are already subscribed to this feed.")

    now = _now()
    settings = get_settings()
    feed = Feed(
        user_id=user.id,
        folder_id=folder_id,
        url=found.feed_url,
        site_url=found.site_url or found.parsed.site_url,
        title=(found.parsed.title or found.feed_url)[:300],
        description=found.parsed.description or "",
        icon_url=found.icon_url,
        websub_hub=found.hub,
        fetch_interval_min=settings.fetch_default_interval_min,
        etag=found.etag,
        last_modified=found.last_modified,
        next_fetch_at=now,
    )
    session.add(feed)
    await session.flush()

    items = await ingest(session, feed, found.parsed, now=now)
    _schedule_success(feed, now, changed=bool(items))
    await session.commit()

    await _notify_new_items(feed, items)
    if folder_id is None:
        try:
            await queue.enqueue(queue.AI_FILE_FEED, str(feed.id))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enqueue %s for feed %s: %s", queue.AI_FILE_FEED, feed.id, exc)
    await _cache_favicon(session, feed)
    return feed


def _conditional_headers(feed: Feed) -> dict[str, str]:
    headers: dict[str, str] = {}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified
    return headers


async def _maybe_follow_permanent_redirect(session: AsyncSession, feed: Feed, response: httpx.Response) -> None:
    if not response.history:
        return
    if not all(r.status_code in PERMANENT_REDIRECTS for r in response.history):
        return
    final_url = str(response.url)[:MAX_URL]
    if final_url == feed.url:
        return
    if await _feed_exists(session, feed.user_id, final_url):
        log.info("feed %s permanently moved to %s but that URL is already subscribed", feed.url, final_url)
        return
    log.info("feed %s permanently moved to %s", feed.url, final_url)
    feed.url = final_url
    # Validators belong to the old resource; a stale ETag against the new URL could 304 forever.
    feed.etag = None
    feed.last_modified = None


def _store_validators(feed: Feed, response: httpx.Response) -> None:
    etag = response.headers.get("etag")
    last_modified = response.headers.get("last-modified")
    if etag is not None or last_modified is not None or response.status_code == 200:
        feed.etag = etag[:512] if etag else None
        feed.last_modified = last_modified[:128] if last_modified else None


async def _refresh(session: AsyncSession, feed: Feed, now: datetime) -> list[Item]:
    try:
        response = await fetch_http.get(feed.url, headers=_conditional_headers(feed))
    except (httpx.HTTPError, fetch_http.UnsafeURLError, OSError) as exc:
        _schedule_error(feed, now, f"{type(exc).__name__}: {exc}")
        return []

    status = response.status_code
    if status == 304:
        # Some servers echo (or rotate) validators on a 304; keep whatever they send, else keep ours.
        _store_validators(feed, response)
        _schedule_success(feed, now, changed=False)
        return []
    if status == 410:
        _schedule_error(feed, now, "Feed is gone (410)")
        feed.paused = True
        return []
    if status in RETRY_AFTER_STATUSES:
        retry_after = parse_retry_after(response.headers.get("retry-after"), now)
        _schedule_error(feed, now, f"HTTP {status}", retry_after=retry_after)
        return []
    if status != 200:
        _schedule_error(feed, now, f"HTTP {status}")
        return []

    await _maybe_follow_permanent_redirect(session, feed, response)
    try:
        parsed = parse_feed(response.content, str(response.url), now=now)
    except ParseError as exc:
        _schedule_error(feed, now, f"parse error: {exc}")
        return []

    items = await ingest(session, feed, parsed, now=now)
    _store_validators(feed, response)
    _schedule_success(feed, now, changed=bool(items))
    return items


async def refresh_feed(session: AsyncSession, feed: Feed) -> list[Item]:
    """Poll ``feed`` once. Records errors/backoff on the row instead of raising; returns the new items.

    Any unexpected exception (a constraint race, an oversized value, a bug) is also recorded as an error
    with backoff so the feed is never left permanently due.
    """
    now = _now()
    try:
        items = await _refresh(session, feed, now)
        await session.commit()
    except Exception as exc:
        log.exception("refresh of feed %s failed unexpectedly", feed.url)
        await session.rollback()
        _schedule_error(feed, now, f"{type(exc).__name__}: {exc}")
        await session.commit()
        return []

    await _notify_new_items(feed, items)
    return items


__all__ = [
    "FeedError",
    "add_feed",
    "apply_rules",
    "backoff_interval",
    "ingest",
    "lengthen_interval",
    "load_rules",
    "parse_retry_after",
    "refresh_feed",
    "shorten_interval",
]
