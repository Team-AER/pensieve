"""Enqueue helpers shared by all packages. Names are the arq function names."""

from __future__ import annotations

import uuid
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from pensieve.config import get_settings

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


# Two queues so a burst of long AI jobs can never hold every worker slot and starve feed polling.
FETCH_QUEUE = "arq:queue"  # arq's default; fetch/reader-mode/prune jobs and their crons
AI_QUEUE = "pensieve:ai"  # everything named ai_*; served by pensieve.worker.AIWorkerSettings


def queue_for(function: str) -> str:
    return AI_QUEUE if function.startswith("ai_") else FETCH_QUEUE


async def enqueue(function: str, *args: Any, _job_id: str | None = None, **kwargs: Any) -> None:
    pool = await get_pool()
    await pool.enqueue_job(function, *args, _job_id=_job_id, _queue_name=queue_for(function), **kwargs)


# Job names (contract between packages). Implementations live in fetch/jobs.py and ai/jobs.py.
FETCH_FEED = "fetch_feed"  # (feed_id: str)
FETCH_READER_MODE = "fetch_reader_mode"  # (item_id: str)
AI_PROCESS_NEW_ITEMS = "ai_process_new_items"  # (feed_id: str, item_ids: list[str]) -> embed + tag + cluster
AI_FILE_FEED = "ai_file_feed"  # (feed_id: str) -> suggest folder
AI_DAILY_DIGEST = "ai_daily_digest"  # (user_id: str)
AI_WEEKLY_REVIEW = "ai_weekly_review"  # (user_id: str)
AI_REFRESH_PROFILE = "ai_refresh_profile"  # (user_id: str)
AI_SUMMARIZE_ITEM = "ai_summarize_item"  # (user_id: str, item_id: str)


def job_id_for(kind: str, target: uuid.UUID | str) -> str:
    """Deterministic ids so a feed or item is never queued twice concurrently."""
    return f"{kind}:{target}"
