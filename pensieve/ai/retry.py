"""Settle and retry failed AI jobs (the Queue card on Manage -> AI and memory).

``ai_jobs`` is a mirror of arq work, so a ``failed`` row stays failed after something else has already done
the work: the summary sweep, a backfill, the next fetch of the same feed. ``reconcile`` settles those rows as
``resolved`` so the failed count shows only work that is still missing. It also turns ``queued`` rows that arq
lost long ago (a worker restart, a Redis flush) into ``failed``, since nothing will ever run them.

``retry_failed`` hands what is still missing to fresh jobs and marks the old rows ``retried``. The mirror keeps
only a job's kind and one target id, not its arguments, so the work is rebuilt from the data: the untagged
items of a feed, the items that still have no summary, and so on. The fresh jobs get their own mirror rows, so
the counts go down as they finish and come back only if they fail again.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models, queue
from pensieve.ai import insights
from pensieve.ai.common import utcnow
from pensieve.config import get_settings

log = logging.getLogger(__name__)

STATUS_RESOLVED = "resolved"
"""A later run wrote what this job could not."""
STATUS_RETRIED = "retried"
"""Handed to a fresh job from the AI page; that job has its own row."""
FAILED_STATUSES = ("failed", "partial")
LOST_AFTER = timedelta(hours=6)
"""A ``queued`` row whose run_after is this far past was dropped by arq and will never run."""
SUMMARY_KINDS = {"summarize_items", "summarize"}
USER_KINDS = {
    "digest": queue.AI_DAILY_DIGEST,
    "weekly": queue.AI_WEEKLY_REVIEW,
    "profile": queue.AI_REFRESH_PROFILE,
    "paper": queue.AI_DAILY_PAPER,
}


def _untagged(feed_id: uuid.UUID, user_id: uuid.UUID, before: datetime | None = None):
    """Items of ``feed_id`` with no item_ai row (never tagged), newest first."""
    stmt = select(models.Item.id).where(
        models.Item.feed_id == feed_id,
        ~select(models.ItemAI.item_id)
        .where(models.ItemAI.item_id == models.Item.id, models.ItemAI.user_id == user_id)
        .exists(),
    )
    if before is not None:
        stmt = stmt.where(models.Item.fetched_at <= before)
    return stmt.order_by(models.Item.published_at.desc())


async def _summary_missing(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID) -> bool:
    item = await session.get(models.Item, item_id)
    if item is None:
        return False  # pruned or unsubscribed: nothing left to summarise
    around = timedelta(seconds=1)
    missing = await insights.items_without_summary(
        session, user_id, item.published_at - around, until=item.published_at + around, limit=50
    )
    return item_id in missing


async def _covered(session: AsyncSession, row: models.AIJob) -> bool:
    """Has the work this failed row stood for been done since?"""
    later_success = await session.scalar(
        select(models.AIJob.id)
        .where(
            models.AIJob.user_id == row.user_id,
            models.AIJob.kind == row.kind,
            models.AIJob.target_id == row.target_id,
            models.AIJob.status == "done",
            models.AIJob.created_at > row.created_at,
        )
        .limit(1)
    )
    if later_success is not None:
        return True
    if row.target_id is None or row.user_id is None:
        return False
    if row.kind == "process_items":
        if await session.get(models.Feed, row.target_id) is None:
            return True
        left = await session.scalar(_untagged(row.target_id, row.user_id, row.created_at).limit(1))
        return left is None
    if row.kind in SUMMARY_KINDS:
        return not await _summary_missing(session, row.user_id, row.target_id)
    return False


async def reconcile(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Settle this user's failed rows whose work is done by now; fail queued rows arq lost. Returns the number
    of rows that changed. The caller commits."""
    now = utcnow()
    changed = 0
    lost = await session.scalars(
        select(models.AIJob).where(
            models.AIJob.user_id == user_id,
            models.AIJob.status == "queued",
            models.AIJob.run_after < now - LOST_AFTER,
        )
    )
    for row in lost:
        row.status = "failed"
        row.last_error = ("lost from the queue; last error: " + (row.last_error or "none"))[:2000]
        row.finished_at = now
        changed += 1
    failed = await session.scalars(
        select(models.AIJob).where(models.AIJob.user_id == user_id, models.AIJob.status.in_(FAILED_STATUSES))
    )
    for row in list(failed):
        if await _covered(session, row):
            row.status = STATUS_RESOLVED
            changed += 1
    if changed:
        await session.flush()
    return changed


def _chunks(ids: list, size: int) -> Iterable[list]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


async def retry_failed(session: AsyncSession, user: models.User) -> dict[str, int]:
    """Queue fresh jobs for every failed row still missing its work; mark those rows ``retried``.

    Returns ``{"jobs": n, "rows": m}``: fresh jobs queued, failed rows handed over. The caller commits.
    """
    await reconcile(session, user.id)
    rows = list(
        await session.scalars(
            select(models.AIJob).where(
                models.AIJob.user_id == user.id, models.AIJob.status.in_(FAILED_STATUSES)
            )
        )
    )
    if not rows:
        return {"jobs": 0, "rows": 0}
    cap = get_settings().ai_max_items_per_job
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    jobs = 0

    async def enqueue(function: str, key: str, *args: object) -> None:
        nonlocal jobs
        await queue.enqueue(function, *args, _job_id=queue.job_id_for(function, f"retry:{key}:{stamp}"))
        jobs += 1

    # Tagging (embed + tag + cluster): every untagged item of each feed that failed.
    feeds = {r.target_id for r in rows if r.kind == "process_items" and r.target_id}
    for feed_id in feeds:
        ids = [str(i) for i in (await session.scalars(_untagged(feed_id, user.id))).all()]
        for n, chunk in enumerate(_chunks(ids, cap)):
            await enqueue(queue.AI_PROCESS_NEW_ITEMS, f"{feed_id}:{n}", str(feed_id), chunk)

    # Summaries: the failed targets, plus whatever else still lacks one around those jobs (a batch job only
    # recorded its first item). The summaries job skips anything a story already covers.
    summary_rows = [r for r in rows if r.kind in SUMMARY_KINDS]
    if summary_rows:
        targets = {r.target_id for r in summary_rows if r.target_id}
        since = min(r.created_at for r in summary_rows) - timedelta(days=2)
        ids = await insights.items_without_summary(session, user.id, since, limit=400)
        for target in targets:
            if target not in ids and await _summary_missing(session, user.id, target):
                ids.append(target)
        for n, chunk in enumerate(_chunks([str(i) for i in ids], cap)):
            await enqueue(queue.AI_SUMMARIZE_ITEMS, f"{user.id}:{n}", str(user.id), chunk)

    # Folder suggestions, per feed; digest, weekly, profile and paper, once each for today.
    for feed_id in {r.target_id for r in rows if r.kind == "file_feed" and r.target_id}:
        if await session.get(models.Feed, feed_id) is not None:
            await enqueue(queue.AI_FILE_FEED, str(feed_id), str(feed_id))
    for kind in {r.kind for r in rows if r.kind in USER_KINDS}:
        await enqueue(USER_KINDS[kind], f"{kind}:{user.id}", str(user.id))

    for row in rows:
        row.status = STATUS_RETRIED
    await session.flush()
    log.info("retry for %s: %d failed rows -> %d jobs", user.email, len(rows), jobs)
    return {"jobs": jobs, "rows": len(rows)}


def failed_filter(user_id: uuid.UUID):
    return and_(models.AIJob.user_id == user_id, models.AIJob.status.in_(FAILED_STATUSES))


def notes_filter(user_id: uuid.UUID):
    """Finished fine, but with something worth knowing (embeddings unavailable, part of a batch covered)."""
    return and_(
        models.AIJob.user_id == user_id, models.AIJob.status == "done", models.AIJob.last_error.is_not(None)
    )


__all__ = ["STATUS_RESOLVED", "STATUS_RETRIED", "failed_filter", "notes_filter", "reconcile", "retry_failed"]
