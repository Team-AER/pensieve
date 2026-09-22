"""Favicon caching: download a feed's icon into ``feeds.icon_data`` so the UI never hot-links third parties."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.config import get_settings
from pensieve.fetch import http as fetch_http
from pensieve.models import Feed

log = logging.getLogger(__name__)

IMAGE_TYPES = ("image/", "application/octet-stream")


def _candidates(feed: Feed) -> list[str]:
    urls: list[str] = []
    if feed.icon_url:
        urls.append(feed.icon_url)
    for base in (feed.site_url, feed.url):
        if not base:
            continue
        parts = urlsplit(base)
        if parts.scheme and parts.netloc:
            candidate = urlunsplit((parts.scheme, parts.netloc, "/favicon.ico", "", ""))
            if candidate not in urls:
                urls.append(candidate)
    return urls


def _looks_like_image(content_type: str, body: bytes) -> bool:
    if (
        body[:4] == b"\x89PNG"
        or body[:3] == b"GIF"
        or body[:2] == b"\xff\xd8"
        or body[:4] == b"\x00\x00\x01\x00"
    ):
        return True
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return True
    head = body[:256].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in body[:1024].lower()):
        return True
    return content_type.startswith("image/") and bool(body)


async def fetch_favicon(feed: Feed, *, client: httpx.AsyncClient | None = None) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` for the first candidate icon that downloads, else ``None``."""
    limit = get_settings().favicon_max_bytes
    for url in _candidates(feed):
        try:
            response = await fetch_http.get(url, client=client, max_bytes=limit)
        except (httpx.HTTPError, fetch_http.UnsafeURLError, OSError) as exc:
            log.debug("favicon %s failed: %s", url, exc)
            continue
        if response.status_code != 200 or not response.content:
            continue
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not _looks_like_image(content_type, response.content):
            continue
        if not content_type.startswith("image/"):
            content_type = "image/x-icon"
        if feed.icon_url != str(response.url) and url != feed.icon_url and not feed.icon_url:
            feed.icon_url = str(response.url)[:2048]
        return response.content, content_type[:100]
    return None


async def refresh_feed_icon(
    session: AsyncSession, feed: Feed, *, client: httpx.AsyncClient | None = None
) -> bool:
    """Best effort: store the feed's favicon bytes on the row. Never raises."""
    try:
        found = await fetch_favicon(feed, client=client)
    except Exception as exc:  # noqa: BLE001 - icons are cosmetic
        log.info("favicon refresh for %s failed: %s", feed.url, exc)
        return False
    if found is None:
        return False
    feed.icon_data, feed.icon_content_type = found
    return True


async def refresh_stale_icons(session: AsyncSession, now: datetime | None = None, limit: int = 500) -> int:
    """Weekly cron body: (re)fetch icons for feeds without one or whose row was not touched recently."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=get_settings().favicon_refresh_days)
    stmt = (
        select(Feed)
        .where(Feed.paused.is_(False))
        .where((Feed.icon_data.is_(None)) | (Feed.updated_at < cutoff))
        .order_by(Feed.updated_at)
        .limit(limit)
    )
    feeds = list(await session.scalars(stmt))
    done = 0
    async with fetch_http.get_client() as client:
        for feed in feeds:
            if await refresh_feed_icon(session, feed, client=client):
                done += 1
            feed.updated_at = now  # touched even on failure so a dead icon host is retried weekly, not hourly
    await session.commit()
    return done


__all__ = ["fetch_favicon", "refresh_feed_icon", "refresh_stale_icons"]
