"""arq job functions for AI work. Each mirrors its status into ``ai_jobs`` and defers when the gateway is down.

Retry policy: gateway unhealthy -> ``Retry(defer=10min)`` (status stays ``queued``); ``LLMError`` -> up to
``MAX_TRIES`` attempts two minutes apart, then ``failed``; any other exception -> ``failed`` immediately. A job
whose last try still has a failed step is mirrored as ``partial`` (some output was written: vectors, item_ai
rows or clusters) or ``failed`` (nothing was), never ``done``.

The ``ai_jobs`` mirror row is keyed on the arq job id (``uuid5(JOB_NS, ctx["job_id"])`` is the row's primary
key) so retries of one job update one row while concurrent chunks of the same feed each keep their own.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from arq import Retry, cron
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models, queue
from pensieve.ai import categorize, cluster, embeddings, insights, memory
from pensieve.ai.client import LLMClient, LLMError
from pensieve.ai.common import ai_on, utcnow
from pensieve.config import get_settings
from pensieve.db import session_scope

log = logging.getLogger(__name__)

HEALTH_DEFER = timedelta(minutes=10)
ERROR_DEFER = timedelta(minutes=2)
MAX_TRIES = 3
JOB_TIMEOUT_S = 1800
"""Must match ``WorkerSettings.job_timeout``; the reaper fails ``running`` mirror rows older than this."""
JOB_NS = uuid.UUID("6f0c2a1e-6b0a-4b7e-9a51-1c1a9d3f0a11")
DISPATCH_MINUTES = {0, 15, 30, 45}
STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


@dataclass
class Outcome:
    """What ``work`` hands back: a note for ``ai_jobs.last_error`` and the terminal status to mirror."""

    note: str | None = None
    status: str = STATUS_DONE


WorkFn = Callable[[AsyncSession, LLMClient], Awaitable["str | Outcome | None"]]


# ---------------------------------------------------------------------------
# ai_jobs mirror + guard
# ---------------------------------------------------------------------------


def mirror_id(ctx: dict, kind: str, target_id: uuid.UUID | None) -> uuid.UUID:
    """Primary key of the mirror row: derived from the arq job id, or from (kind, target) when there is none."""
    job_id = ctx.get("job_id") if isinstance(ctx, dict) else None
    return uuid.uuid5(JOB_NS, str(job_id) if job_id else f"{kind}:{target_id}")


async def _mirror(
    row_id: uuid.UUID,
    kind: str,
    target_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    *,
    status: str,
    attempts: int,
    last_error: str | None = None,
    run_after: datetime | None = None,
    tokens: tuple[int, int] | None = None,
    finished: bool = False,
) -> uuid.UUID:
    async with session_scope() as session:
        row = await session.get(models.AIJob, row_id)
        if row is None:
            row = models.AIJob(id=row_id, kind=kind, target_id=target_id, user_id=user_id)
            session.add(row)
        elif row.status in (STATUS_DONE, STATUS_PARTIAL, STATUS_FAILED) and attempts <= 1:
            # the same job id re-enqueued after arq forgot it: start the row's history over
            row.tokens_in = row.tokens_out = 0
            row.finished_at = None
            row.created_at = utcnow()
        row.status = status
        row.attempts = attempts
        row.last_error = last_error
        now = utcnow()
        if run_after is not None:
            row.run_after = run_after
        if status == "running":
            row.started_at = now
        if finished:
            row.finished_at = now
        if tokens:
            row.tokens_in += tokens[0]
            row.tokens_out += tokens[1]
        await session.flush()
        return row.id


async def _guarded(
    ctx: dict,
    kind: str,
    target_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    work: WorkFn,
) -> None:
    """Run ``work`` with health check, ai_jobs mirroring and retry policy.

    ``work`` returns a note (str/None, mirrored as ``done``) or an ``Outcome`` carrying its own terminal status.
    """
    attempt = int(ctx.get("job_try") or 1)
    row_id = mirror_id(ctx, kind, target_id)
    client = LLMClient()
    try:
        health = await client.health()
        if not health["ok"]:
            await _mirror(
                row_id,
                kind,
                target_id,
                user_id,
                status="queued",
                attempts=attempt,
                last_error="gateway unhealthy",
                run_after=utcnow() + HEALTH_DEFER,
            )
            raise Retry(defer=HEALTH_DEFER)
        await _mirror(row_id, kind, target_id, user_id, status="running", attempts=attempt)
        try:
            async with session_scope() as session:
                result = await work(session, client)
        except LLMError as exc:
            tokens = client.take_usage()
            if attempt < MAX_TRIES:
                await _mirror(
                    row_id,
                    kind,
                    target_id,
                    user_id,
                    status="queued",
                    attempts=attempt,
                    last_error=str(exc)[:2000],
                    run_after=utcnow() + ERROR_DEFER,
                    tokens=tokens,
                )
                raise Retry(defer=ERROR_DEFER) from exc
            await _mirror(
                row_id,
                kind,
                target_id,
                user_id,
                status=STATUS_FAILED,
                attempts=attempt,
                last_error=str(exc)[:2000],
                tokens=tokens,
                finished=True,
            )
            raise
        except Retry:
            raise
        except Exception as exc:
            await _mirror(
                row_id,
                kind,
                target_id,
                user_id,
                status=STATUS_FAILED,
                attempts=attempt,
                last_error=repr(exc)[:2000],
                tokens=client.take_usage(),
                finished=True,
            )
            raise
        outcome = result if isinstance(result, Outcome) else Outcome(note=result)
        if outcome.status != STATUS_DONE:
            log.warning("ai job %s (%s) ended %s: %s", kind, target_id, outcome.status, outcome.note)
        await _mirror(
            row_id,
            kind,
            target_id,
            user_id,
            status=outcome.status,
            attempts=attempt,
            last_error=outcome.note,
            tokens=client.take_usage(),
            finished=True,
        )
    finally:
        await client.aclose()


async def reap_stale_jobs(older_than_s: int = JOB_TIMEOUT_S) -> int:
    """Mark ``running`` mirror rows that started more than ``older_than_s`` ago as failed (worker died or arq
    timed the job out, neither of which reaches ``_guarded``). Returns the number of rows reaped."""
    cutoff = utcnow() - timedelta(seconds=older_than_s)
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(models.AIJob).where(
                    models.AIJob.status == "running",
                    models.AIJob.started_at.is_not(None),
                    models.AIJob.started_at < cutoff,
                )
            )
        ).all()
        for row in rows:
            row.status = STATUS_FAILED
            row.last_error = f"reaped: still running after {older_than_s}s (worker restart or job timeout)"
            row.finished_at = utcnow()
        await session.flush()
        if rows:
            log.warning("reaped %d stale running ai_jobs rows", len(rows))
        return len(rows)


async def _load_user(session: AsyncSession, user_id: uuid.UUID) -> models.User | None:
    return await session.get(models.User, user_id)


# ---------------------------------------------------------------------------
# Job functions (names are the constants in pensieve.queue)
# ---------------------------------------------------------------------------


async def _feed_owner(feed_id: uuid.UUID) -> uuid.UUID | None:
    """Feed jobs are mirrored under the feed's owner so the Manage page's queue counts are per user."""
    async with session_scope() as session:
        return await session.scalar(select(models.Feed.user_id).where(models.Feed.id == feed_id))


def remainder_job_id(feed_id: uuid.UUID | str, n: int, chunk: list[str]) -> str:
    """Deterministic id for follow-up chunk ``n`` of a capped job; the first item id disambiguates so a fresh
    overflow for the same feed never collides with (and is silently dropped by) a chunk still in the queue."""
    return queue.job_id_for("process", f"{feed_id}:{n}:{chunk[0][:8]}")


async def _enqueue_remainder(ctx: dict, feed_id: uuid.UUID, rest: list[models.Item], cap: int) -> int:
    """Queue the items beyond the cap as follow-up jobs of ``cap`` items each. Returns the number of jobs."""
    ids = [str(i.id) for i in rest]
    jobs = 0
    for n, start in enumerate(range(0, len(ids), cap), start=1):
        chunk = ids[start : start + cap]
        await _enqueue(ctx, queue.AI_PROCESS_NEW_ITEMS, str(feed_id), chunk, job_id=remainder_job_id(feed_id, n, chunk))
        jobs += 1
    return jobs


async def ai_process_new_items(ctx: dict, feed_id: str, item_ids: list[str]) -> None:
    """Embed -> tag -> cluster new items of one feed, each step independently guarded.

    Only the newest ``ai_max_items_per_job`` items are processed here; the rest are re-queued in cap-sized
    chunks so a first fetch of a deep archive is processed in order without flooding the gateway.
    """
    fid = uuid.UUID(str(feed_id))
    ids = [uuid.UUID(str(i)) for i in item_ids]

    async def work(session: AsyncSession, client: LLMClient) -> str | Outcome | None:
        feed = await session.get(models.Feed, fid)
        if feed is None or not ids:
            return "feed or items missing"
        user = await _load_user(session, feed.user_id)
        if user is None or not ai_on(user):
            return "ai disabled"
        items = list((await session.scalars(select(models.Item).where(models.Item.id.in_(ids)))).all())
        items = [i for i in items if i.feed_id == fid]
        if not items:
            return "no items"
        notes: list[str] = []
        cap = get_settings().ai_max_items_per_job
        if len(items) > cap:
            items.sort(key=lambda i: i.published_at, reverse=True)
            items, rest = items[:cap], items[cap:]
            try:
                n_jobs = await _enqueue_remainder(ctx, fid, rest, cap)
                notes.append(f"processed newest {cap}; {len(rest)} more re-queued in {n_jobs} job(s)")
            except Exception as exc:  # noqa: BLE001 - Redis hiccup must not fail the items we do have
                log.warning("could not re-queue %d remaining items for feed %s: %s", len(rest), fid, exc)
                notes.append(
                    f"processed newest {cap}; could not re-queue {len(rest)} more ({exc.__class__.__name__}); "
                    "run `python -m pensieve.ai backfill`"
                )
        errors: list[LLMError] = []
        wrote = False  # did any step produce output? decides partial vs failed on the last try
        try:
            if await embeddings.embed_items(session, items, client) is None:
                notes.append("embeddings unavailable")
            else:
                wrote = True
        except LLMError as exc:
            errors.append(exc)
            notes.append(f"embed failed: {exc}")
        try:
            wrote |= bool(await categorize.tag_items(session, user, items, client))
        except LLMError as exc:
            errors.append(exc)
            notes.append(f"tag failed: {exc}")
        try:
            wrote |= bool(await cluster.cluster_items(session, user, items, client))
        except LLMError as exc:
            errors.append(exc)
            notes.append(f"cluster failed: {exc}")
        await session.commit()
        note = "; ".join(notes)[:2000] or None
        if errors and int(ctx.get("job_try") or 1) < MAX_TRIES:
            raise errors[0]
        if errors:
            return Outcome(note=note, status=STATUS_PARTIAL if wrote else STATUS_FAILED)
        return note

    await _guarded(ctx, "process_items", fid, await _feed_owner(fid), work)


async def ai_file_feed(ctx: dict, feed_id: str) -> None:
    fid = uuid.UUID(str(feed_id))

    async def work(session: AsyncSession, client: LLMClient) -> str | None:
        feed = await session.get(models.Feed, fid)
        if feed is None:
            return "feed missing"
        user = await _load_user(session, feed.user_id)
        if user is None or not ai_on(user):
            return "ai disabled"
        folder = await categorize.file_feed(session, user, feed, client)
        return f"suggested {folder.name}" if folder else "no suggestion"

    await _guarded(ctx, "file_feed", fid, await _feed_owner(fid), work)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001 - a bad tz name must not stop the worker
        return ZoneInfo("UTC")


def _today() -> date:
    return datetime.now(_tz()).date()


async def ai_daily_digest(ctx: dict, user_id: str, day: str | None = None) -> None:
    uid = uuid.UUID(str(user_id))
    target_day = date.fromisoformat(day) if day else _today()

    async def work(session: AsyncSession, client: LLMClient) -> str | None:
        user = await _load_user(session, uid)
        if user is None or not ai_on(user, "digest"):
            return "digest disabled"
        row = await insights.daily_digest(session, user, target_day, client)
        return f"insight {row.id}" if row else None

    await _guarded(ctx, "digest", uid, uid, work)


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


async def ai_weekly_review(ctx: dict, user_id: str, iso_week: str | None = None) -> None:
    uid = uuid.UUID(str(user_id))
    week = iso_week or _iso_week(_today())

    async def work(session: AsyncSession, client: LLMClient) -> str | None:
        user = await _load_user(session, uid)
        if user is None or not ai_on(user, "digest"):
            return "digest disabled"
        row = await insights.weekly_review(session, user, week, client)
        return f"insight {row.id}" if row else None

    await _guarded(ctx, "weekly", uid, uid, work)


async def ai_refresh_profile(ctx: dict, user_id: str) -> None:
    uid = uuid.UUID(str(user_id))

    async def work(session: AsyncSession, client: LLMClient) -> str | None:
        user = await _load_user(session, uid)
        if user is None or not ai_on(user, "memory"):
            return "memory disabled"
        row = await memory.refresh_profile(session, user, client)
        return f"profile v{row.version}" if row else None

    await _guarded(ctx, "profile", uid, uid, work)


async def ai_summarize_item(ctx: dict, user_id: str, item_id: str) -> None:
    uid = uuid.UUID(str(user_id))
    iid = uuid.UUID(str(item_id))

    async def work(session: AsyncSession, client: LLMClient) -> str | None:
        user = await _load_user(session, uid)
        item = await session.get(models.Item, iid)
        if user is None or item is None or not ai_on(user):
            return "skipped"
        feed = await session.get(models.Feed, item.feed_id)
        if feed is None or feed.user_id != user.id:
            return "item not owned by user"
        await insights.summarize_item(session, user, item, client)
        return None

    await _guarded(ctx, "summarize", iid, uid, work)


# ---------------------------------------------------------------------------
# Cron dispatchers
# ---------------------------------------------------------------------------


def digest_job_id(user_id: uuid.UUID | str, day: date | None = None) -> str:
    """Day-qualified id shared by the cron and the manual "generate" button so one day runs once."""
    return queue.job_id_for(queue.AI_DAILY_DIGEST, f"{user_id}:{(day or _today()).isoformat()}")


def weekly_job_id(user_id: uuid.UUID | str, iso_week: str | None = None) -> str:
    return queue.job_id_for(queue.AI_WEEKLY_REVIEW, f"{user_id}:{iso_week or _iso_week(_today())}")


async def _enqueue(ctx: dict, function: str, *args: object, job_id: str) -> None:
    redis = ctx.get("redis") if isinstance(ctx, dict) else None
    if redis is not None:
        job = await redis.enqueue_job(function, *args, _job_id=job_id)
    else:
        job = await queue.enqueue(function, *args, _job_id=job_id)
    if job is None and function == queue.AI_PROCESS_NEW_ITEMS:
        log.warning("%s %s already queued; duplicate dropped by arq", function, job_id)


async def _users_with(session: AsyncSession, toggle: str) -> list[models.User]:
    users = (await session.scalars(select(models.User).where(models.User.ai_enabled.is_(True)))).all()
    return [u for u in users if ai_on(u, toggle)]


def user_digest_time(user: models.User) -> tuple[int, int]:
    """(hour, minute) from ``user.settings["digest_time"]`` ("HH:MM"), else the global default."""
    s = get_settings()
    raw = str((user.settings or {}).get("digest_time") or "")
    try:
        hh, mm = raw.split(":")
        hour, minute = int(hh), int(mm)
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    return s.digest_hour_local, s.digest_minute_local


def digest_due(user: models.User, at: datetime) -> bool:
    """True when ``at`` (in ``settings.timezone``) falls in the 15-minute slot of the user's digest time."""
    hour, minute = user_digest_time(user)
    return at.hour == hour and at.minute // 15 == minute // 15


async def ai_dispatch_daily(ctx: dict, at: datetime | None = None) -> int:
    """Every 15 minutes: enqueue today's digest for users whose digest time falls in this slot.

    Also reaps stale ``running`` mirror rows. ``at`` overrides the clock (tests, manual catch-up).
    """
    try:
        await reap_stale_jobs()
    except Exception as exc:  # noqa: BLE001 - housekeeping must not block dispatch
        log.warning("reaper failed: %s", exc)
    now_local = (at or datetime.now(_tz())).astimezone(_tz())
    n = 0
    async with session_scope() as session:
        for user in await _users_with(session, "digest"):
            if not digest_due(user, now_local):
                continue
            await _enqueue(
                ctx,
                queue.AI_DAILY_DIGEST,
                str(user.id),
                job_id=digest_job_id(user.id, now_local.date()),
            )
            n += 1
    return n


async def ai_dispatch_weekly(ctx: dict) -> int:
    """Sunday: weekly review + profile refresh for every user."""
    week = _iso_week(_today())
    n = 0
    async with session_scope() as session:
        for user in await _users_with(session, "digest"):
            await _enqueue(ctx, queue.AI_WEEKLY_REVIEW, str(user.id), job_id=weekly_job_id(user.id, week))
            n += 1
        for user in await _users_with(session, "memory"):
            await _enqueue(
                ctx,
                queue.AI_REFRESH_PROFILE,
                str(user.id),
                job_id=queue.job_id_for(queue.AI_REFRESH_PROFILE, f"{user.id}:{week}"),
            )
    return n


def _cron_jobs() -> list:
    """Wall-clock schedules in ``settings.timezone``: the worker sets ``WorkerSettings.timezone`` to the same
    zone, so no conversion (and no dependence on the clock at import time) is needed."""
    return [
        cron(ai_dispatch_daily, minute=DISPATCH_MINUTES, unique=True),
        cron(ai_dispatch_weekly, weekday=6, hour=8, minute=0, unique=True),
    ]


FUNCTIONS: list = [
    ai_process_new_items,
    ai_file_feed,
    ai_daily_digest,
    ai_weekly_review,
    ai_refresh_profile,
    ai_summarize_item,
]
CRON_JOBS: list = _cron_jobs()
