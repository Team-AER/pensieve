"""arq job functions for AI work. Each mirrors its status into ``ai_jobs`` and defers when the gateway is down.

Retry policy: gateway unhealthy -> ``Retry(defer=10min)`` (status stays ``queued``); ``LLMError`` -> up to
``MAX_TRIES`` attempts two minutes apart, then ``failed``; any other exception -> ``failed`` immediately.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
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


# ---------------------------------------------------------------------------
# ai_jobs mirror + guard
# ---------------------------------------------------------------------------


async def _mirror(
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
        row = await session.scalar(
            select(models.AIJob)
            .where(
                models.AIJob.kind == kind,
                models.AIJob.target_id == target_id,
                models.AIJob.status.in_(["queued", "running"]),
            )
            .order_by(models.AIJob.created_at.desc())
            .limit(1)
        )
        if row is None:
            row = models.AIJob(kind=kind, target_id=target_id, user_id=user_id)
            session.add(row)
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
    work: Callable[[AsyncSession, LLMClient], Awaitable[str | None]],
) -> None:
    """Run ``work`` with health check, ai_jobs mirroring and retry policy. ``work`` returns an optional note."""
    attempt = int(ctx.get("job_try") or 1)
    client = LLMClient()
    try:
        health = await client.health()
        if not health["ok"]:
            await _mirror(
                kind,
                target_id,
                user_id,
                status="queued",
                attempts=attempt,
                last_error="gateway unhealthy",
                run_after=utcnow() + HEALTH_DEFER,
            )
            raise Retry(defer=HEALTH_DEFER)
        await _mirror(kind, target_id, user_id, status="running", attempts=attempt)
        try:
            async with session_scope() as session:
                note = await work(session, client)
        except LLMError as exc:
            tokens = client.take_usage()
            if attempt < MAX_TRIES:
                await _mirror(
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
                kind,
                target_id,
                user_id,
                status="failed",
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
                kind,
                target_id,
                user_id,
                status="failed",
                attempts=attempt,
                last_error=repr(exc)[:2000],
                tokens=client.take_usage(),
                finished=True,
            )
            raise
        await _mirror(
            kind,
            target_id,
            user_id,
            status="done",
            attempts=attempt,
            last_error=note,
            tokens=client.take_usage(),
            finished=True,
        )
    finally:
        await client.aclose()


async def _load_user(session: AsyncSession, user_id: uuid.UUID) -> models.User | None:
    return await session.get(models.User, user_id)


# ---------------------------------------------------------------------------
# Job functions (names are the constants in pensieve.queue)
# ---------------------------------------------------------------------------


async def _feed_owner(feed_id: uuid.UUID) -> uuid.UUID | None:
    """Feed jobs are mirrored under the feed's owner so the Manage page's queue counts are per user."""
    async with session_scope() as session:
        return await session.scalar(select(models.Feed.user_id).where(models.Feed.id == feed_id))


async def ai_process_new_items(ctx: dict, feed_id: str, item_ids: list[str]) -> None:
    """Embed -> tag -> cluster new items of one feed, each step independently guarded."""
    fid = uuid.UUID(str(feed_id))
    ids = [uuid.UUID(str(i)) for i in item_ids]

    async def work(session: AsyncSession, client: LLMClient) -> str | None:
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
            notes.append(
                f"capped to newest {cap} of {len(items)}; run `python -m pensieve.ai backfill` for the rest"
            )
            items = items[:cap]
        errors: list[LLMError] = []
        try:
            if await embeddings.embed_items(session, items, client) is None:
                notes.append("embeddings unavailable")
        except LLMError as exc:
            errors.append(exc)
            notes.append(f"embed failed: {exc}")
        try:
            await categorize.tag_items(session, user, items, client)
        except LLMError as exc:
            errors.append(exc)
            notes.append(f"tag failed: {exc}")
        try:
            await cluster.cluster_items(session, user, items, client)
        except LLMError as exc:
            errors.append(exc)
            notes.append(f"cluster failed: {exc}")
        await session.commit()
        if errors and int(ctx.get("job_try") or 1) < MAX_TRIES:
            raise errors[0]
        return "; ".join(notes)[:2000] or None

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


def _today() -> date:
    try:
        return datetime.now(ZoneInfo(get_settings().timezone)).date()
    except Exception:  # noqa: BLE001
        return utcnow().date()


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


async def _enqueue(ctx: dict, function: str, *args: str, job_id: str) -> None:
    redis = ctx.get("redis") if isinstance(ctx, dict) else None
    if redis is not None:
        await redis.enqueue_job(function, *args, _job_id=job_id)
    else:
        await queue.enqueue(function, *args, _job_id=job_id)


async def _users_with(session: AsyncSession, toggle: str) -> list[models.User]:
    users = (await session.scalars(select(models.User).where(models.User.ai_enabled.is_(True)))).all()
    return [u for u in users if ai_on(u, toggle)]


async def ai_dispatch_daily(ctx: dict) -> int:
    """Enqueue today's digest for every user with digests enabled."""
    today = _today().isoformat()
    n = 0
    async with session_scope() as session:
        for user in await _users_with(session, "digest"):
            await _enqueue(
                ctx,
                queue.AI_DAILY_DIGEST,
                str(user.id),
                job_id=queue.job_id_for(queue.AI_DAILY_DIGEST, f"{user.id}:{today}"),
            )
            n += 1
    return n


async def ai_dispatch_weekly(ctx: dict) -> int:
    """Sunday: weekly review + profile refresh for every user."""
    week = _iso_week(_today())
    n = 0
    async with session_scope() as session:
        for user in await _users_with(session, "digest"):
            await _enqueue(
                ctx,
                queue.AI_WEEKLY_REVIEW,
                str(user.id),
                job_id=queue.job_id_for(queue.AI_WEEKLY_REVIEW, f"{user.id}:{week}"),
            )
            n += 1
        for user in await _users_with(session, "memory"):
            await _enqueue(
                ctx,
                queue.AI_REFRESH_PROFILE,
                str(user.id),
                job_id=queue.job_id_for(queue.AI_REFRESH_PROFILE, f"{user.id}:{week}"),
            )
    return n


def _worker_local(hour: int, minute: int, weekday: int | None = None) -> tuple[int, int, int | None]:
    """Convert a wall-clock time in ``settings.timezone`` into the worker's local timezone (arq cron uses it).

    Foundation could instead set ``WorkerSettings.timezone = ZoneInfo(settings.timezone)``; until then we translate.
    """
    try:
        tz = ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    anchor = datetime.now(tz).date()
    if weekday is not None:
        anchor += timedelta(days=(weekday - anchor.weekday()) % 7)
    local = datetime.combine(anchor, time(hour, minute), tzinfo=tz).astimezone()
    return local.hour, local.minute, (local.weekday() if weekday is not None else None)


def _cron_jobs() -> list:
    s = get_settings()
    h, m, _ = _worker_local(s.digest_hour_local, s.digest_minute_local)
    wh, wm, wd = _worker_local(8, 0, weekday=6)
    return [
        cron(ai_dispatch_daily, hour=h, minute=m, unique=True),
        cron(ai_dispatch_weekly, weekday=wd, hour=wh, minute=wm, unique=True),
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
