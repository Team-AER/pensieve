"""Durable AI queue: every ``ai_*`` job is written to ``ai_jobs`` before it reaches arq, and sent again if arq loses it.

arq keeps a job only in Redis and, by default, drops one that has not started within a day, which a deep backlog on
a slow local model reaches. Here the row is the record and Redis only carries it: jobs go to arq with ``NO_EXPIRY``,
``requeue_lost`` re-sends queued rows whose arq job is gone, and ``adopt`` gives a row (and the same long expiry) to
jobs already in Redis that were queued before the ledger existed or by a path that bypassed it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from arq.constants import in_progress_key_prefix, job_key_prefix
from arq.jobs import deserialize_job
from sqlalchemy import select

from pensieve import models, queue
from pensieve.ai.common import utcnow
from pensieve.db import session_scope

log = logging.getLogger(__name__)

JOB_NS = uuid.UUID("6f0c2a1e-6b0a-4b7e-9a51-1c1a9d3f0a11")
NO_EXPIRY = timedelta(days=365)
LOST_GRACE = timedelta(minutes=5)
"""A queued row younger than this is not checked against Redis (its enqueue may still be on the way)."""

# arq function -> the ai_jobs kind its mirror row carries (see each job's ``_guarded`` call)
KINDS: dict[str, str] = {
    queue.AI_PROCESS_NEW_ITEMS: "process_items",
    queue.AI_FILE_FEED: "file_feed",
    queue.AI_DAILY_DIGEST: "digest",
    queue.AI_WEEKLY_REVIEW: "weekly",
    queue.AI_REFRESH_PROFILE: "profile",
    queue.AI_DAILY_PAPER: "paper",
    queue.AI_SUMMARIZE_ITEMS: "summarize_items",
    queue.AI_SUMMARIZE_ITEM: "summarize",
}
ITEM_LIST_KINDS = ("process_items", "summarize_items")
"""Kinds whose second argument is a list of item ids (counted as articles waiting)."""


def row_id_for(job_id: str) -> uuid.UUID:
    return uuid.uuid5(JOB_NS, job_id)


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


async def _describe(session, function: str, args: list) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    """(kind, target_id, user_id) exactly as the job's own mirror update will write them."""
    kind = KINDS[function]
    if kind in ("process_items", "file_feed"):
        feed = await session.get(models.Feed, _uuid(args[0]))
        return kind, _uuid(args[0]), feed.user_id if feed else None
    user_id = _uuid(args[0])
    if kind == "summarize_items":
        return kind, _uuid(args[1][0]) if args[1] else None, user_id
    if kind == "summarize":
        return kind, _uuid(args[1]), user_id
    return kind, user_id, user_id


async def record(function: str, args: list, job_id: str, *, keep_existing: bool = False) -> None:
    """Write (or restart) the ledger row for one queued job. A row already queued or running is left alone."""
    async with session_scope() as session:
        row = await session.get(models.AIJob, row_id_for(job_id))
        if row is not None and (keep_existing or row.status in ("queued", "running")):
            return
        kind, target_id, user_id = await _describe(session, function, args)
        if row is None:
            row = models.AIJob(id=row_id_for(job_id))
            session.add(row)
        now = utcnow()
        row.kind, row.target_id, row.user_id = kind, target_id, user_id
        row.status, row.attempts, row.last_error = "queued", 0, None
        row.function, row.args, row.job_id = function, args, job_id
        row.run_after = row.created_at = now
        row.started_at = row.finished_at = None
        row.tokens_in = row.tokens_out = 0


async def _send(redis, function: str, args: list, job_id: str | None):
    return await redis.enqueue_job(
        function, *args, _job_id=job_id, _queue_name=queue.queue_for(function), _expires=NO_EXPIRY
    )


async def enqueue(function: str, *args: Any, job_id: str | None = None, redis=None):
    """Queue an AI job durably. Returns arq's Job, or None when arq already holds that job id."""
    redis = redis or await queue.get_pool()
    if function not in KINDS:
        return await _send(redis, function, list(args), job_id)
    job_id = job_id or f"{function}:{uuid.uuid4().hex}"
    await record(function, list(args), job_id)
    return await _send(redis, function, list(args), job_id)


async def requeue_lost(redis) -> int:
    """Send again every queued ledger row whose arq job is gone. Returns how many were sent."""
    cutoff = utcnow() - LOST_GRACE
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(models.AIJob).where(
                    models.AIJob.status == "queued",
                    models.AIJob.function.is_not(None),
                    models.AIJob.run_after < cutoff,
                )
            )
        ).all()
        pending = [(r.function, list(r.args or []), r.job_id) for r in rows]
    sent = 0
    for function, args, job_id in pending:
        if await redis.exists(job_key_prefix + job_id, in_progress_key_prefix + job_id):
            continue
        if await _send(redis, function, args, job_id) is not None:
            sent += 1
    if sent:
        log.warning("ledger: re-sent %d queued AI jobs arq had lost", sent)
    return sent


async def adopt(redis) -> int:
    """Give every AI job waiting in Redis a ledger row and the long expiry. Returns how many rows were written."""
    written = 0
    for raw in await redis.zrange(queue.AI_QUEUE, 0, -1):
        job_id = raw.decode() if isinstance(raw, bytes) else str(raw)
        payload = await redis.get(job_key_prefix + job_id)
        if payload is None:
            continue
        await redis.pexpire(job_key_prefix + job_id, int(NO_EXPIRY.total_seconds() * 1000))
        job = deserialize_job(payload)
        if job.function not in KINDS:
            continue
        async with session_scope() as session:
            row = await session.get(models.AIJob, row_id_for(job_id))
            if row is not None:
                if (
                    row.function is None
                ):  # a mirror row from before the ledger: keep its state, add the payload
                    row.function, row.args, row.job_id = job.function, list(job.args), job_id
                continue
        await record(job.function, list(job.args), job_id, keep_existing=True)
        written += 1
    if written:
        log.info("ledger: adopted %d AI jobs already waiting in Redis", written)
    return written


__all__ = ["JOB_NS", "KINDS", "NO_EXPIRY", "adopt", "enqueue", "record", "requeue_lost", "row_id_for"]
