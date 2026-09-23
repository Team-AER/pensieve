"""arq workers. Job functions live in their packages; this file only assembles them.

Two processes, two queues:
  `arq pensieve.worker.WorkerSettings`    fetch worker  (feed polling, reader mode, retention; default queue)
  `python -m pensieve.worker ai`          AI worker     (tagging, clustering, digests; queue ``pensieve:ai``)
  `arq pensieve.worker.CaptureWorkerSettings`  capture worker (saved pages via the browser; ``pensieve:capture``)
A single pool used to run both, and four half-hour tagging jobs would hold every slot while the
per-minute fetch cron waited behind them for over an hour.
"""

from __future__ import annotations

import logging
import logging.config
import sys
from zoneinfo import ZoneInfo

from arq.connections import RedisSettings
from arq.logs import default_log_config
from arq.worker import Worker, get_kwargs

from pensieve import queue
from pensieve.ai.jobs import CRON_JOBS as AI_CRON_JOBS
from pensieve.ai.jobs import FUNCTIONS as AI_FUNCTIONS
from pensieve.ai.jobs import JOB_TIMEOUT_S, reap_stale_jobs
from pensieve.archive.jobs import CRON_JOBS as CAPTURE_CRON_JOBS
from pensieve.archive.jobs import FUNCTIONS as CAPTURE_FUNCTIONS
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


async def ai_startup(ctx: dict) -> None:
    get_engine()
    try:
        # jobs that were running when the previous worker died never reach their own status update
        await reap_stale_jobs(older_than_s=JOB_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - housekeeping must not stop the worker
        log.warning("startup reaper failed: %s", exc)


async def shutdown(ctx: dict) -> None:
    await dispose_engine()


class WorkerSettings:
    """Fetch worker: many short network jobs on arq's default queue."""

    queue_name = queue.FETCH_QUEUE
    functions = [*FETCH_FUNCTIONS]
    cron_jobs = [*FETCH_CRON_JOBS]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    timezone = _timezone()
    max_jobs = 8
    job_timeout = 300
    keep_result = 60


class AIWorkerSettings:
    """AI worker: few long gateway-bound jobs; concurrency per model is capped inside ``LLMClient``.

    Run it with ``python -m pensieve.worker ai`` so ``max_jobs`` follows the Gateway card; plain ``arq`` still
    works but keeps the environment's value until restarted.
    """

    queue_name = queue.AI_QUEUE
    functions = [*AI_FUNCTIONS]
    cron_jobs = [*AI_CRON_JOBS]
    on_startup = ai_startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    timezone = _timezone()
    max_jobs = max(1, get_settings().ai_max_jobs)
    job_timeout = JOB_TIMEOUT_S
    keep_result = 60


class CaptureWorkerSettings:
    """Capture worker: a few browser-bound page captures at a time (the browser container does the heavy part)."""

    queue_name = queue.CAPTURE_QUEUE
    functions = [*CAPTURE_FUNCTIONS]
    cron_jobs = [*CAPTURE_CRON_JOBS]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    timezone = _timezone()
    max_jobs = max(1, get_settings().capture_concurrency)
    job_timeout = 240
    keep_result = (
        1  # the job id is capture_page:<snapshot>; a re-capture right after must not be deduplicated
    )


class AIWorker(Worker):
    """An arq worker whose job limit follows the Gateway card (``ai_jobs``) without a restart.

    arq checks ``job_counter < max_jobs`` before it starts each job, so lowering ``max_jobs`` stops new starts
    until running jobs drain, and raising it (up to the ceiling the semaphore was built for) starts more at the
    next poll. The override row is re-read at most once a minute.
    """

    async def _poll_iteration(self) -> None:
        from pensieve.ai import model_choice

        await model_choice.apply_overrides()
        self.max_jobs = min(max(1, get_settings().ai_max_jobs), model_choice.MAX_COUNT)
        await super()._poll_iteration()


def run_ai_worker() -> None:
    from pensieve.ai import model_choice

    logging.config.dictConfig(default_log_config(verbose=False))
    AIWorker(**(get_kwargs(AIWorkerSettings) | {"max_jobs": model_choice.MAX_COUNT})).run()


if __name__ == "__main__":
    if sys.argv[1:] != ["ai"]:
        sys.exit("usage: python -m pensieve.worker ai")
    run_ai_worker()
