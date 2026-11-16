"""Read-only discovery, cached in Redis. Previewing never writes subscriptions or items."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from pensieve.config import get_settings
from pensieve.fetch.discover import DiscoveryError, discover

log = logging.getLogger(__name__)


def volume(entries, now):
    dates = sorted(
        {e.guid: e.published_at for e in entries if e.date_known and e.published_at <= now}.values()
    )
    # A truncated feed spanning under a day cannot establish a useful rate.
    if len(dates) < 2 or (dates[-1] - dates[0]).total_seconds() < 86400:
        return None
    return round((len(dates) - 1) * 7 * 86400 / (dates[-1] - dates[0]).total_seconds(), 1)


async def preview(url, *, use_cache=True):
    settings = get_settings()
    key = "pensieve:discover:v1:" + hashlib.sha256(url.encode()).hexdigest()
    now = datetime.now(UTC)
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        try:
            cached = await redis.get(key)
            if cached and use_cache:
                data = json.loads(cached)
                stamp = datetime.fromisoformat(data["measured_at"])
                if 0 <= (now - stamp).total_seconds() < settings.discovery_cache_seconds:
                    return data
        except (RedisError, OSError, ValueError, KeyError, TypeError) as exc:
            log.debug("Discovery cache miss: %s", exc)
        try:
            found = await discover(url)
            data = {
                "url": url,
                "feed_url": found.feed_url,
                "title": found.parsed.title,
                "measured_at": now.isoformat(),
                "posts_per_week": volume(found.parsed.entries, now),
                "sample_size": len(found.parsed.entries),
                "entries": [
                    {
                        "title": e.title,
                        "url": e.url if e.url and urlsplit(e.url).scheme in {"http", "https"} else None,
                        "published_at": e.published_at.isoformat(),
                        "hash": e.hash,
                        "text": e.content_text[:6000],
                    }
                    for e in sorted(found.parsed.entries, key=lambda e: e.published_at, reverse=True)[:40]
                ],
                "error": None,
            }
        except (DiscoveryError, httpx.HTTPError, OSError, ValueError) as exc:
            data = {
                "url": url,
                "feed_url": url,
                "title": url,
                "measured_at": now.isoformat(),
                "posts_per_week": None,
                "sample_size": 0,
                "entries": [],
                "error": str(exc)[:300],
            }
        try:
            await redis.set(
                key, json.dumps(data), ex=300 if data["error"] else settings.discovery_cache_seconds
            )
        except (RedisError, OSError) as exc:
            log.debug("Could not cache discovery: %s", exc)
        return data
    finally:
        await redis.aclose()
