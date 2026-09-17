"""Failed-sign-in rate limiting keyed by (client ip, email), backed by Redis with an in-memory fallback.

Used by ``POST /login`` and the Google Reader ``/accounts/ClientLogin``. ``settings.login_rate_limit_attempts``
failures inside ``settings.login_rate_limit_window_s`` block further attempts (429 + Retry-After) until the
window expires; a successful sign-in clears the counter. A Redis outage fails open (logged), never closed.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from pensieve.config import get_settings

log = logging.getLogger(__name__)

Key = tuple[str, str]


class RateLimiter(Protocol):
    async def blocked_for(self, key: Key) -> int:
        """Seconds until the key may try again; 0 when not blocked."""

    async def record_failure(self, key: Key) -> None: ...

    async def reset(self, key: Key) -> None: ...


def _redis_key(key: Key) -> str:
    ip, email = key
    return f"pensieve:login-fail:{ip}:{email.lower()}"


class MemoryRateLimiter:
    """Process-local limiter: the fallback when Redis is unreachable, and what tests inject."""

    def __init__(self, attempts: int | None = None, window_s: int | None = None) -> None:
        settings = get_settings()
        self.attempts = attempts or settings.login_rate_limit_attempts
        self.window_s = window_s or settings.login_rate_limit_window_s
        self._hits: dict[str, tuple[int, float]] = {}

    def _live(self, key: Key) -> tuple[int, float] | None:
        entry = self._hits.get(_redis_key(key))
        if entry is None:
            return None
        count, expires = entry
        if expires <= time.monotonic():
            self._hits.pop(_redis_key(key), None)
            return None
        return count, expires

    async def blocked_for(self, key: Key) -> int:
        live = self._live(key)
        if live is None or live[0] < self.attempts:
            return 0
        return max(1, int(live[1] - time.monotonic()))

    async def record_failure(self, key: Key) -> None:
        live = self._live(key)
        if live is None:
            self._hits[_redis_key(key)] = (1, time.monotonic() + self.window_s)
        else:
            self._hits[_redis_key(key)] = (live[0] + 1, live[1])

    async def reset(self, key: Key) -> None:
        self._hits.pop(_redis_key(key), None)


class RedisRateLimiter:
    """INCR + EXPIRE counter per key; shared across web workers."""

    def __init__(self, url: str, attempts: int, window_s: int) -> None:
        self.url = url
        self.attempts = attempts
        self.window_s = window_s
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self.url, socket_connect_timeout=1.0, socket_timeout=1.0)
        return self._client

    async def blocked_for(self, key: Key) -> int:
        try:
            r = self._redis()
            name = _redis_key(key)
            count = await r.get(name)
            if count is None or int(count) < self.attempts:
                return 0
            ttl = await r.ttl(name)
            return max(1, int(ttl)) if ttl and ttl > 0 else self.window_s
        except Exception as exc:  # noqa: BLE001 - fail open
            log.warning("rate limiter unavailable (%s); allowing sign-in attempt", exc)
            return 0

    async def record_failure(self, key: Key) -> None:
        try:
            r = self._redis()
            name = _redis_key(key)
            count = await r.incr(name)
            if count == 1:
                await r.expire(name, self.window_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("rate limiter unavailable (%s); failure not recorded", exc)

    async def reset(self, key: Key) -> None:
        try:
            await self._redis().delete(_redis_key(key))
        except Exception as exc:  # noqa: BLE001
            log.warning("rate limiter unavailable (%s); counter not reset", exc)


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """The process-wide limiter (Redis by default). Tests replace it with :func:`set_limiter`."""
    global _limiter
    if _limiter is None:
        settings = get_settings()
        _limiter = RedisRateLimiter(
            settings.redis_url, settings.login_rate_limit_attempts, settings.login_rate_limit_window_s
        )
    return _limiter


def set_limiter(limiter: RateLimiter | None) -> None:
    global _limiter
    _limiter = limiter


def client_ip(request) -> str:
    return request.client.host if request.client else "unknown"


__all__ = ["MemoryRateLimiter", "RateLimiter", "RedisRateLimiter", "client_ip", "get_limiter", "set_limiter"]
