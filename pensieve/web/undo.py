"""Undo batches for mark-all-read: ids live in Redis (10 minutes) under a random token; only the token
travels through the form. Falls back to a bounded in-process dict when Redis is unreachable."""

from __future__ import annotations

import logging
import secrets
import time
import uuid

from pensieve.config import get_settings

log = logging.getLogger(__name__)

TTL_S = 600
_memory: dict[str, tuple[list[uuid.UUID], float]] = {}
_MEMORY_MAX = 1000
_redis = None


def _client():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(get_settings().redis_url, socket_connect_timeout=1.0, socket_timeout=1.0)
    return _redis


def _prune_memory() -> None:
    now = time.monotonic()
    for key in [k for k, (_, exp) in _memory.items() if exp <= now]:
        _memory.pop(key, None)
    while len(_memory) > _MEMORY_MAX:
        _memory.pop(next(iter(_memory)))


async def save_undo(user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> str | None:
    """Store up to ``settings.undo_batch_max_ids`` ids; returns the token (``None`` when nothing to undo)."""
    if not item_ids:
        return None
    ids = item_ids[: get_settings().undo_batch_max_ids]
    token = secrets.token_urlsafe(16)
    payload = ",".join(str(i) for i in ids)
    try:
        await _client().set(f"pensieve:undo:{user_id}:{token}", payload, ex=TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.debug("undo store: redis unavailable (%s); using memory", exc)
        _prune_memory()
        _memory[f"{user_id}:{token}"] = (ids, time.monotonic() + TTL_S)
    return token


async def load_undo(user_id: uuid.UUID, token: str | None) -> list[uuid.UUID]:
    """Ids saved under ``token`` for this user (consumed on read), else ``[]``."""
    if not token or not token.replace("-", "").replace("_", "").isalnum():
        return []
    name = f"pensieve:undo:{user_id}:{token}"
    try:
        raw = await _client().getdel(name)
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        return [uuid.UUID(p) for p in text.split(",") if p]
    entry = _memory.pop(f"{user_id}:{token}", None)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return []


__all__ = ["load_undo", "save_undo"]
