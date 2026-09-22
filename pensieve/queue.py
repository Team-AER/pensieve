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
CAPTURE_QUEUE = "pensieve:capture"  # everything named capture_*; pensieve.worker.CaptureWorkerSettings (browser)


def queue_for(function: str) -> str:
    if function.startswith("ai_"):
        return AI_QUEUE
    if function.startswith("capture_") or function == "import_links":
        return CAPTURE_QUEUE
    return FETCH_QUEUE


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
AI_SUMMARIZE_ITEM = "ai_summarize_item"  # (user_id: str, item_id: str, hint: str = "") -> one call, or a rewrite
AI_SUMMARIZE_ITEMS = "ai_summarize_items"  # (user_id: str, item_ids: list[str]) -> eager batch, one per story
AI_DAILY_PAPER = "ai_daily_paper"  # (user_id: str, day: str | None) -> compile today's paper (no LLM)
AI_SUMMARY_SWEEP = "ai_summary_sweep"  # cron: re-queue summaries the eager job could not write
CAPTURE_PAGE = "capture_page"  # (snapshot_id: str) -> fetch, render, freeze and extract one saved page
CAPTURE_IMPORT = "import_links"  # (user_id: str, links: list[dict]) -> a Pocket/Instapaper/bookmarks import


def job_id_for(kind: str, target: uuid.UUID | str) -> str:
    """Deterministic ids so a feed or item is never queued twice concurrently."""
    return f"{kind}:{target}"
