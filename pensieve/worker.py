"""arq worker. Job functions live in their packages; this file only assembles them.

Run: `arq pensieve.worker.WorkerSettings`
"""

from __future__ import annotations

from arq.connections import RedisSettings

from pensieve.ai.jobs import CRON_JOBS as AI_CRON_JOBS
from pensieve.ai.jobs import FUNCTIONS as AI_FUNCTIONS
from pensieve.config import get_settings
from pensieve.db import dispose_engine, get_engine
from pensieve.fetch.jobs import CRON_JOBS as FETCH_CRON_JOBS
from pensieve.fetch.jobs import FUNCTIONS as FETCH_FUNCTIONS


async def startup(ctx: dict) -> None:
    get_engine()


async def shutdown(ctx: dict) -> None:
    await dispose_engine()


class WorkerSettings:
    functions = [*FETCH_FUNCTIONS, *AI_FUNCTIONS]
    cron_jobs = [*FETCH_CRON_JOBS, *AI_CRON_JOBS]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 8
    job_timeout = 600
    keep_result = 60
