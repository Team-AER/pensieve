"""Shared, user-scoped query helpers for the web package."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from pensieve.models import Feed, Item, ItemState, User

# ---------------------------------------------------------------------------
# Nav count cache: the sidebar's unread/starred/tag counts are the most expensive queries on every page
# and change only on state writes (which invalidate) or new items (10 s of staleness is fine).
# ---------------------------------------------------------------------------

NAV_CACHE_TTL_S = 10.0


@dataclass
class NavCounts:
    unread_by_feed: dict[uuid.UUID, int] = field(default_factory=dict)
    starred: int = 0
    user_tag_counts: dict[str, int] = field(default_factory=dict)
    ai_tag_counts: dict[str, int] = field(default_factory=dict)


_nav_cache: dict[uuid.UUID, tuple[float, NavCounts]] = {}


def get_cached_nav_counts(user_id: uuid.UUID) -> NavCounts | None:
    entry = _nav_cache.get(user_id)
    if entry is None:
        return None
    stamp, counts = entry
    if time.monotonic() - stamp > NAV_CACHE_TTL_S:
        _nav_cache.pop(user_id, None)
        return None
    return counts


def set_cached_nav_counts(user_id: uuid.UUID, counts: NavCounts) -> None:
    _nav_cache[user_id] = (time.monotonic(), counts)


def invalidate_nav_cache(user_id: uuid.UUID | None = None) -> None:
    """Forget cached counts for one user (or everyone). Called on every item_states write."""
    if user_id is None:
        _nav_cache.clear()
    else:
        _nav_cache.pop(user_id, None)


def parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def not_hidden(state=ItemState):
    return or_(state.hidden.is_(None), state.hidden.is_(False))


def is_unread(state=ItemState):
    return or_(state.is_read.is_(None), state.is_read.is_(False))


async def get_user_item(session: AsyncSession, user: User, item_id: uuid.UUID) -> tuple[Item, Feed] | None:
    row = (
        await session.execute(
            select(Item, Feed).join(Feed, Item.feed_id == Feed.id).where(Item.id == item_id, Feed.user_id == user.id)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def get_state(session: AsyncSession, user: User, item_id: uuid.UUID) -> ItemState | None:
    return await session.get(ItemState, (user.id, item_id))


async def upsert_states(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID], **values: Any) -> None:
    """Insert-or-update item_states for many items at once (a missing row means unread/unstarred)."""
    if not item_ids:
        return
    rows = [
        {"user_id": user_id, "item_id": iid, "is_read": False, "is_starred": False, "hidden": False, "tags": []}
        | values
        for iid in item_ids
    ]
    stmt = pg_insert(ItemState).values(rows)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id", "item_id"], set_=values)
    await session.execute(stmt)
    invalidate_nav_cache(user_id)
    # Core writes bypass the identity map: expire any already-loaded state rows so re-reads see the new values.
    _expire_states(session, user_id, item_ids)


def _expire_states(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> None:
    for iid in item_ids:
        obj = session.identity_map.get(identity_key(ItemState, (user_id, iid)))
        if obj is not None:
            session.expire(obj)


async def mark_read_where(session: AsyncSession, user_id: uuid.UUID, item_ids_select) -> list[uuid.UUID]:
    """Mark every item selected by ``item_ids_select`` read with one server-side
    ``INSERT ... SELECT ... ON CONFLICT DO UPDATE``; returns the affected ids (for Undo)."""
    from pensieve.syncapi.common import mark_read_from_select

    result = await session.execute(mark_read_from_select(user_id, item_ids_select, datetime.now(UTC)))
    ids = [row[0] for row in result.all()]
    invalidate_nav_cache(user_id)
    _expire_states(session, user_id, ids)
    return ids


async def set_read(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID], read: bool) -> None:
    now = datetime.now(UTC)
    await upsert_states(session, user_id, item_ids, is_read=read, read_at=now if read else None)


async def set_starred(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID, starred: bool) -> None:
    now = datetime.now(UTC)
    await upsert_states(session, user_id, [item_id], is_starred=starred, starred_at=now if starred else None)


async def user_owns_items(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Filter a list of item ids down to those in the user's own feeds."""
    if not item_ids:
        return []
    rows = await session.scalars(
        select(Item.id).join(Feed, Item.feed_id == Feed.id).where(Feed.user_id == user_id, Item.id.in_(item_ids))
    )
    return list(rows)


async def unread_state_ids(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Of the given items, those the user has not read yet."""
    if not item_ids:
        return []
    rows = await session.execute(
        select(Item.id)
        .outerjoin(ItemState, and_(ItemState.item_id == Item.id, ItemState.user_id == user_id))
        .where(Item.id.in_(item_ids), is_unread())
    )
    return [r[0] for r in rows]


async def undo_read(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> int:
    if not item_ids:
        return 0
    result = await session.execute(
        update(ItemState)
        .where(ItemState.user_id == user_id, ItemState.item_id.in_(item_ids))
        .values(is_read=False, read_at=None)
    )
    invalidate_nav_cache(user_id)
    _expire_states(session, user_id, item_ids)
    return result.rowcount or 0
