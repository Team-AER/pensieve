"""Subscribing to feeds and polling them: conditional GET, item insertion, rules, adaptive scheduling."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

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
MAX_ERROR_TEXT = 1000


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


def _schedule_error(feed: Feed, now: datetime, error: str) -> None:
    feed.error_count = (feed.error_count or 0) + 1
    feed.last_error = error[:MAX_ERROR_TEXT]
    feed.last_fetch_at = now
    feed.fetch_interval_min = backoff_interval(feed.error_count)
    feed.next_fetch_at = now + timedelta(minutes=feed.fetch_interval_min)
    if feed.error_count >= MAX_CONSECUTIVE_ERRORS:
        feed.paused = True
        log.warning("feed %s paused after %d consecutive errors: %s", feed.url, feed.error_count, error)
    else:
        log.info("feed %s error %d: %s", feed.url, feed.error_count, error)


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
    return Item(
        id=uuid.uuid4(),
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


async def ingest(session: AsyncSession, feed: Feed, parsed: ParsedFeed, *, now: datetime | None = None) -> list[Item]:
    """Insert entries not yet stored for this feed (by guid), apply rules, return the new ``Item`` rows."""
    now = now or _now()
    _update_feed_metadata(feed, parsed)

    fresh: dict[str, ParsedEntry] = {}
    for entry in parsed.entries:
        if entry.guid and entry.guid not in fresh:
            fresh[entry.guid] = entry
    if not fresh:
        return []

    existing = set(
        (await session.scalars(select(Item.guid).where(Item.feed_id == feed.id, Item.guid.in_(fresh)))).all()
    )
    items = [_entry_to_item(feed, entry) for guid, entry in fresh.items() if guid not in existing]
    if not items:
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
        await queue.enqueue(queue.AI_PROCESS_NEW_ITEMS, str(feed.id), [str(i.id) for i in items])
    except Exception as exc:  # noqa: BLE001 - a missing Redis must never fail a fetch
        log.warning("could not enqueue %s for feed %s: %s", queue.AI_PROCESS_NEW_ITEMS, feed.id, exc)


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
        id=uuid.uuid4(),
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
    final_url = str(response.url)
    if final_url == feed.url:
        return
    if await _feed_exists(session, feed.user_id, final_url):
        log.info("feed %s permanently moved to %s but that URL is already subscribed", feed.url, final_url)
        return
    log.info("feed %s permanently moved to %s", feed.url, final_url)
    feed.url = final_url


async def refresh_feed(session: AsyncSession, feed: Feed) -> list[Item]:
    """Poll ``feed`` once. Records errors/backoff on the row instead of raising; returns the new items."""
    now = _now()
    try:
        response = await fetch_http.get(feed.url, headers=_conditional_headers(feed))
    except (httpx.HTTPError, fetch_http.UnsafeURLError, OSError) as exc:
        _schedule_error(feed, now, f"{type(exc).__name__}: {exc}")
        await session.commit()
        return []

    if response.status_code == 304:
        _schedule_success(feed, now, changed=False)
        await session.commit()
        return []
    if response.status_code != 200:
        _schedule_error(feed, now, f"HTTP {response.status_code}")
        await session.commit()
        return []

    await _maybe_follow_permanent_redirect(session, feed, response)
    try:
        parsed = parse_feed(response.content, str(response.url), now=now)
    except ParseError as exc:
        _schedule_error(feed, now, f"parse error: {exc}")
        await session.commit()
        return []

    items = await ingest(session, feed, parsed, now=now)
    feed.etag = response.headers.get("etag")
    feed.last_modified = response.headers.get("last-modified")
    _schedule_success(feed, now, changed=bool(items))
    await session.commit()

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
    "refresh_feed",
    "shorten_interval",
]
