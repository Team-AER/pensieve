"""Helpers shared across the AI package: toggles, time, vector maths, reader-state approximations."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.config import get_settings

MARK_ALL_BATCH_MIN = 5
"""A 'mark all read' batch is approximated as >= 5 item_states of one user sharing the same read_at second.

The reader writes read_at per row; an individual open produces a lone timestamp, whereas mark-all/mark-above
writes many rows in one statement with the same clock value. Items read inside such a batch count as *skipped*
(read without opening); everything else read counts as *opened*.
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def ai_on(user: models.User, toggle: str | None = None) -> bool:
    """Global switch AND the user's switch AND (optionally) a per-feature toggle in ``user.settings``."""
    if not get_settings().ai_enabled or not user.ai_enabled:
        return False
    if toggle is None:
        return True
    return bool((user.settings or {}).get(toggle, True))


# -- vectors ------------------------------------------------------------------------------------------------


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dims = len(vectors[0])
    out = [0.0] * dims
    for v in vectors:
        for i in range(dims):
            out[i] += v[i]
    n = float(len(vectors))
    return [x / n for x in out]


def counter_cosine(a: Counter, b: Counter) -> float:
    keys = set(a) | set(b)
    return cosine([float(a.get(k, 0)) for k in keys], [float(b.get(k, 0)) for k in keys])


# -- queries ------------------------------------------------------------------------------------------------


def user_feed_ids(user_id) -> Select:
    return select(models.Feed.id).where(models.Feed.user_id == user_id)


def user_items_query(user_id) -> Select:
    return select(models.Item).where(models.Item.feed_id.in_(user_feed_ids(user_id)))


async def mark_all_batch_times(session: AsyncSession, user_id, since: datetime) -> set[datetime]:
    """Second-truncated read_at values that look like a bulk mark-all (see ``MARK_ALL_BATCH_MIN``)."""
    bucket = func.date_trunc("second", models.ItemState.read_at)
    stmt = (
        select(bucket)
        .where(
            models.ItemState.user_id == user_id,
            models.ItemState.is_read.is_(True),
            models.ItemState.read_at.is_not(None),
            models.ItemState.read_at >= since,
        )
        .group_by(bucket)
        .having(func.count() >= MARK_ALL_BATCH_MIN)
    )
    return {row[0] for row in (await session.execute(stmt)).all()}


def is_batch_read(state: models.ItemState, batches: set[datetime]) -> bool:
    if not state.read_at:
        return False
    return state.read_at.replace(microsecond=0) in batches


async def read_states(session: AsyncSession, user_id, since: datetime) -> list[models.ItemState]:
    stmt = select(models.ItemState).where(
        models.ItemState.user_id == user_id,
        models.ItemState.is_read.is_(True),
        models.ItemState.read_at >= since,
    )
    return list((await session.scalars(stmt)).all())


def window(hours: int | None = None, days: int | None = None) -> datetime:
    delta = timedelta(hours=hours or 0, days=days or 0)
    return utcnow() - delta
