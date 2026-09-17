"""arq worker. Job functions live in their packages; this file only assembles them.

Run: `arq pensieve.worker.WorkerSettings`
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from arq.connections import RedisSettings

from pensieve.ai.jobs import CRON_JOBS as AI_CRON_JOBS
from pensieve.ai.jobs import FUNCTIONS as AI_FUNCTIONS
from pensieve.ai.jobs import JOB_TIMEOUT_S, reap_stale_jobs
from pensieve.config import get_settings
from pensieve.db import dispose_engine, get_engine
from pensieve.fetch.jobs import CRON_JOBS as FETCH_CRON_JOBS
from pensieve.fetch.jobs import FUNCTIONS as FETCH_FUNCTIONS

log = logging.getLogger(__name__)


def _timezone() -> ZoneInfo:
    """Cron schedules are wall-clock times in ``settings.timezone``; a bad name falls back to UTC, loudly."""
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001
        log.error("invalid PENSIEVE_TIMEZONE %r; cron schedules run in UTC", get_settings().timezone)
        return ZoneInfo("UTC")


async def startup(ctx: dict) -> None:
    get_engine()
    try:
        # jobs that were running when the previous worker died never reach their own status update
        await reap_stale_jobs(older_than_s=JOB_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - housekeeping must not stop the worker
        log.warning("startup reaper failed: %s", exc)


async def shutdown(ctx: dict) -> None:
    await dispose_engine()


class WorkerSettings:
    functions = [*FETCH_FUNCTIONS, *AI_FUNCTIONS]
    cron_jobs = [*FETCH_CRON_JOBS, *AI_CRON_JOBS]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    timezone = _timezone()
    max_jobs = 4  # AI jobs serialise on one GPU anyway; fewer slots means fewer half-done jobs on restart
    job_timeout = JOB_TIMEOUT_S
    keep_result = 60
