"""Shared helpers for the sync APIs (Google Reader and Fever).

Item / feed / folder ids
------------------------
Sync clients need 64-bit integer ids while Pensieve uses UUIDs. `long_id(uuid)` derives a stable
non-negative int64 from the first 8 bytes of the UUID (top bit masked off). `long_id_sql(column)` is the
same computation as a SQL expression so the reverse lookup ("which item has long id N?") is a plain
WHERE clause; nothing is stored.

Reader state
------------
`item_states` is written with INSERT ... ON CONFLICT so a missing row (== unread, unstarred) and an
existing row are handled by one statement. Every query in this package is scoped by `user_id`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Text,
    and_,
    cast,
    exists,
    func,
    literal_column,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import BIT
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import ImmutableMultiDict

from pensieve.auth import user_from_api_token
from pensieve.db import get_session
from pensieve.models import Feed, Folder, Item, ItemAI, ItemState, User

INT63_MASK = (1 << 63) - 1
ITEM_TAG_PREFIX = "tag:google.com,2005:reader/item/"

STREAM_READING_LIST = "user/-/state/com.google/reading-list"
STREAM_STARRED = "user/-/state/com.google/starred"
STREAM_READ = "user/-/state/com.google/read"
STREAM_KEPT_UNREAD = "user/-/state/com.google/kept-unread"
LABEL_PREFIX = "user/-/label/"
AI_LABEL_PREFIX = "AI: "

BAD_TOKEN_HEADERS = {"X-Reader-Google-Bad-Token": "true"}

DbSession = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------


def long_id(value: uuid.UUID) -> int:
    """Stable non-negative int64 derived from a UUID (first 8 bytes, top bit cleared)."""
    return int.from_bytes(value.bytes[:8], "big") & INT63_MASK


def item_long_id(item_id: uuid.UUID) -> int:
    return long_id(item_id)


def item_hex_id(item_id: uuid.UUID) -> str:
    """16-char zero-padded hex, as used in `tag:google.com,2005:reader/item/<hex>`."""
    return f"{long_id(item_id):016x}"


def item_tag_id(item_id: uuid.UUID) -> str:
    return ITEM_TAG_PREFIX + item_hex_id(item_id)


def long_id_sql(column: ColumnElement) -> ColumnElement[int]:
    """SQL equivalent of `long_id()` for a UUID column: ('x' || first16hex)::bit(64)::bigint & INT63_MASK."""
    hex16 = func.substr(func.replace(cast(column, Text), "-", ""), 1, 16)
    as_bigint = cast(cast(literal_column("'x'").op("||")(hex16), BIT(64)), BigInteger)
    return as_bigint.op("&")(literal_column(str(INT63_MASK)))


def parse_item_id(raw: str) -> int | None:
    """Accept `tag:google.com,2005:reader/item/<hex>`, bare 16-hex, or a decimal long id."""
    raw = raw.strip()
    if raw.startswith(ITEM_TAG_PREFIX):
        raw = raw[len(ITEM_TAG_PREFIX) :]
        try:
            return int(raw, 16) & INT63_MASK
        except ValueError:
            return None
    try:
        return int(raw, 10) & INT63_MASK
    except ValueError:
        pass
    if len(raw) == 16:
        try:
            return int(raw, 16) & INT63_MASK
        except ValueError:
            return None
    return None


async def items_by_long_ids(session: AsyncSession, user_id: uuid.UUID, ids: list[int]) -> list[uuid.UUID]:
    """Resolve derived ids back to item UUIDs, scoped to the user's feeds."""
    if not ids:
        return []
    rows = await session.scalars(
        select(Item.id)
        .join(Feed, Item.feed_id == Feed.id)
        .where(Feed.user_id == user_id, long_id_sql(Item.id).in_(ids))
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def to_usec(dt: datetime) -> int:
    return int(dt.timestamp()) * 1_000_000 + dt.microsecond


def to_msec(dt: datetime) -> int:
    return to_usec(dt) // 1000


def to_sec(dt: datetime) -> int:
    return int(dt.timestamp())


def from_usec(usec: int) -> datetime:
    return datetime.fromtimestamp(0, UTC) + timedelta(microseconds=usec)


def parse_timestamp(raw: str | None) -> datetime | None:
    """Clients send seconds, milliseconds or microseconds; pick by magnitude."""
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    if value >= 10**15:
        return from_usec(value)
    if value >= 10**12:
        return from_usec(value * 1000)
    return from_usec(value * 1_000_000)


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

StreamKind = Literal["reading-list", "starred", "read", "kept-unread", "label", "feed", "unknown"]


@dataclass(frozen=True)
class Stream:
    kind: StreamKind
    value: str = ""

    @property
    def is_state(self) -> bool:
        return self.kind in {"reading-list", "starred", "read", "kept-unread"}


def parse_stream(raw: str | None) -> Stream:
    raw = (raw or "").strip()
    raw = re.sub(r"^user/[^/]*/", "user/-/", raw)
    if raw in ("", STREAM_READING_LIST):
        return Stream("reading-list")
    if raw == STREAM_STARRED:
        return Stream("starred")
    if raw == STREAM_READ:
        return Stream("read")
    if raw == STREAM_KEPT_UNREAD:
        return Stream("kept-unread")
    if raw.startswith(LABEL_PREFIX) and len(raw) > len(LABEL_PREFIX):
        return Stream("label", raw[len(LABEL_PREFIX) :])
    if raw.startswith("feed/") and len(raw) > 5:
        return Stream("feed", raw[5:])
    return Stream("unknown", raw)


def feed_stream_id(feed_id: uuid.UUID) -> str:
    return f"feed/{feed_id}"


def label_stream_id(name: str) -> str:
    return LABEL_PREFIX + name


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _token_from_header(value: str) -> str | None:
    lowered = value.lower()
    if lowered.startswith("googlelogin "):
        for part in value[len("googlelogin ") :].split(","):
            key, _, val = part.strip().partition("=")
            if key.strip().lower() == "auth" and val:
                return val.strip()
        return None
    if lowered.startswith("bearer "):
        return value[7:].strip() or None
    return None


async def greader_user(request: Request, session: DbSession) -> User:
    """`Authorization: GoogleLogin auth=<token>`; 401 + X-Reader-Google-Bad-Token on failure."""
    token = _token_from_header(request.headers.get("authorization", ""))
    user = await user_from_api_token(token, session) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers=BAD_TOKEN_HEADERS)
    return user


ReaderUser = Annotated[User, Depends(greader_user)]


async def read_params(request: Request) -> ImmutableMultiDict:
    """Query string merged with the form body (clients mix the two freely)."""
    items = list(request.query_params.multi_items())
    if request.method in {"POST", "PUT"}:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type or not content_type:
            try:
                form = await request.form()
            except Exception:  # noqa: BLE001 - malformed body is treated as empty
                form = None
            if form is not None:
                items.extend((k, v) for k, v in form.multi_items() if isinstance(v, str))
    return ImmutableMultiDict(items)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def feed_by_ref(session: AsyncSession, user_id: uuid.UUID, ref: str) -> Feed | None:
    """`ref` is a feed UUID or a feed URL (with or without the `feed/` prefix)."""
    ref = ref.removeprefix("feed/")
    try:
        feed_id = uuid.UUID(ref)
    except ValueError:
        feed_id = None
    if feed_id is not None:
        return await session.scalar(select(Feed).where(Feed.user_id == user_id, Feed.id == feed_id))
    return await session.scalar(select(Feed).where(Feed.user_id == user_id, Feed.url == ref))


async def folder_by_name(session: AsyncSession, user_id: uuid.UUID, name: str) -> Folder | None:
    return await session.scalar(select(Folder).where(Folder.user_id == user_id, Folder.name == name))


async def get_or_create_folder(session: AsyncSession, user_id: uuid.UUID, name: str) -> Folder:
    folder = await folder_by_name(session, user_id, name)
    if folder is None:
        position = await session.scalar(
            select(func.coalesce(func.max(Folder.position), 0)).where(Folder.user_id == user_id)
        )
        folder = Folder(user_id=user_id, name=name, position=(position or 0) + 1)
        session.add(folder)
        await session.flush()
    return folder


async def folder_map(session: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, Folder]:
    rows = await session.scalars(
        select(Folder).where(Folder.user_id == user_id).order_by(Folder.position, Folder.name)
    )
    return {f.id: f for f in rows}


# ---------------------------------------------------------------------------
# Item queries
# ---------------------------------------------------------------------------


def is_unread_expr() -> ColumnElement[bool]:
    return func.coalesce(ItemState.is_read, False).is_(False)


def is_read_expr() -> ColumnElement[bool]:
    return func.coalesce(ItemState.is_read, False).is_(True)


def is_starred_expr() -> ColumnElement[bool]:
    return func.coalesce(ItemState.is_starred, False).is_(True)


def not_hidden_expr() -> ColumnElement[bool]:
    return func.coalesce(ItemState.hidden, False).is_(False)


def visible_items(user_id: uuid.UUID):
    """SELECT Item, Feed, ItemState for the user's feeds, hidden items excluded."""
    return (
        select(Item, Feed, ItemState)
        .join(Feed, Item.feed_id == Feed.id)
        .outerjoin(ItemState, and_(ItemState.item_id == Item.id, ItemState.user_id == user_id))
        .where(Feed.user_id == user_id, not_hidden_expr())
    )


async def stream_filter(
    session: AsyncSession, user_id: uuid.UUID, stream: Stream
) -> ColumnElement[bool] | None:
    """WHERE clause selecting a stream's items over `visible_items`; None means the stream is empty."""
    if stream.kind == "reading-list":
        return literal_column("true")
    if stream.kind == "starred":
        return is_starred_expr()
    if stream.kind == "read":
        return is_read_expr()
    if stream.kind == "kept-unread":
        return is_unread_expr()
    if stream.kind == "feed":
        feed = await feed_by_ref(session, user_id, stream.value)
        return Item.feed_id == feed.id if feed is not None else None
    if stream.kind == "label":
        folder = await folder_by_name(session, user_id, stream.value)
        if folder is not None:
            return Feed.folder_id == folder.id
        if stream.value.startswith(AI_LABEL_PREFIX):
            tag = stream.value[len(AI_LABEL_PREFIX) :]
            return exists().where(ItemAI.user_id == user_id, ItemAI.item_id == Item.id, ItemAI.tags.any(tag))
        return ItemState.tags.any(stream.value)
    return None


@dataclass
class StreamPage:
    rows: list[tuple[Item, Feed, ItemState | None]]
    continuation: str | None


def _encode_continuation(item: Item) -> str:
    return f"{to_usec(item.published_at)}.{item.id.hex}"


def _decode_continuation(raw: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not raw:
        return None
    try:
        usec, hexid = raw.split(".", 1)
        return from_usec(int(usec)), uuid.UUID(hex=hexid)
    except ValueError:
        return None


async def fetch_stream(
    session: AsyncSession,
    user_id: uuid.UUID,
    stream: Stream,
    *,
    n: int,
    oldest_first: bool = False,
    older_than: datetime | None = None,
    newer_than: datetime | None = None,
    exclude: Stream | None = None,
    include: Stream | None = None,
    continuation: str | None = None,
) -> StreamPage:
    where = await stream_filter(session, user_id, stream)
    if where is None:
        return StreamPage([], None)
    stmt = visible_items(user_id).where(where)
    for extra, negate in ((exclude, True), (include, False)):
        if extra is None:
            continue
        clause = await stream_filter(session, user_id, extra)
        if clause is None:
            if not negate:
                return StreamPage([], None)
            continue
        stmt = stmt.where(~clause if negate else clause)
    if older_than is not None:
        stmt = stmt.where(Item.published_at >= older_than)
    if newer_than is not None:
        stmt = stmt.where(Item.published_at <= newer_than)
    cursor = _decode_continuation(continuation)
    if cursor is not None:
        key = tuple_(Item.published_at, Item.id)
        stmt = stmt.where(key > cursor if oldest_first else key < cursor)
    if oldest_first:
        stmt = stmt.order_by(Item.published_at.asc(), Item.id.asc())
    else:
        stmt = stmt.order_by(Item.published_at.desc(), Item.id.desc())
    stmt = stmt.limit(n + 1)
    rows = [(item, feed, state) for item, feed, state in (await session.execute(stmt)).all()]
    next_token = None
    if len(rows) > n:
        rows = rows[:n]
        next_token = _encode_continuation(rows[-1][0])
    return StreamPage(rows, next_token)


async def ai_tags_for(
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not item_ids:
        return {}
    rows = await session.execute(
        select(ItemAI.item_id, ItemAI.tags).where(ItemAI.user_id == user_id, ItemAI.item_id.in_(item_ids))
    )
    return {item_id: list(tags or []) for item_id, tags in rows}


# ---------------------------------------------------------------------------
# Unread counts
# ---------------------------------------------------------------------------


@dataclass
class FeedUnread:
    feed_id: uuid.UUID
    folder_id: uuid.UUID | None
    count: int
    newest: datetime | None


async def unread_by_feed(session: AsyncSession, user_id: uuid.UUID) -> list[FeedUnread]:
    """Unread (no read state) and not hidden, per feed; feeds with zero unread are included with count 0."""
    unread = (
        select(
            Item.feed_id.label("feed_id"),
            func.count(Item.id).label("n"),
            func.max(Item.published_at).label("newest"),
        )
        .join(Feed, Item.feed_id == Feed.id)
        .outerjoin(ItemState, and_(ItemState.item_id == Item.id, ItemState.user_id == user_id))
        .where(Feed.user_id == user_id, is_unread_expr(), not_hidden_expr())
        .group_by(Item.feed_id)
        .subquery()
    )
    stmt = (
        select(Feed.id, Feed.folder_id, func.coalesce(unread.c.n, 0), unread.c.newest)
        .outerjoin(unread, unread.c.feed_id == Feed.id)
        .where(Feed.user_id == user_id)
        .order_by(Feed.position, Feed.title)
    )
    return [
        FeedUnread(fid, folder_id, int(n), newest)
        for fid, folder_id, n, newest in (await session.execute(stmt))
    ]


# ---------------------------------------------------------------------------
# State writes
# ---------------------------------------------------------------------------


def _base_rows(user_id: uuid.UUID, item_ids: list[uuid.UUID], **overrides) -> list[dict]:
    now = datetime.now(UTC)
    rows = []
    for item_id in item_ids:
        row = {
            "user_id": user_id,
            "item_id": item_id,
            "is_read": False,
            "is_starred": False,
            "read_at": None,
            "starred_at": None,
            "hidden": False,
            "tags": [],
            "updated_at": now,
        }
        row.update(overrides)
        rows.append(row)
    return rows


async def set_read(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID], read: bool) -> None:
    if not item_ids:
        return
    now = datetime.now(UTC)
    stmt = pg_insert(ItemState).values(
        _base_rows(user_id, item_ids, is_read=read, read_at=now if read else None)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ItemState.user_id, ItemState.item_id],
        set_={"is_read": read, "read_at": now if read else None, "updated_at": now},
    )
    await session.execute(stmt)


async def set_starred(
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID], starred: bool
) -> None:
    if not item_ids:
        return
    now = datetime.now(UTC)
    stmt = pg_insert(ItemState).values(
        _base_rows(user_id, item_ids, is_starred=starred, starred_at=now if starred else None)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ItemState.user_id, ItemState.item_id],
        set_={"is_starred": starred, "starred_at": now if starred else None, "updated_at": now},
    )
    await session.execute(stmt)


async def edit_tags(
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID], add: list[str], remove: list[str]
) -> None:
    """Add/remove user tags on item_states.tags (creates rows as needed, keeps order, no duplicates)."""
    if not item_ids or not (add or remove):
        return
    existing = {
        s.item_id: s
        for s in await session.scalars(
            select(ItemState).where(ItemState.user_id == user_id, ItemState.item_id.in_(item_ids))
        )
    }
    now = datetime.now(UTC)
    for item_id in item_ids:
        state = existing.get(item_id)
        current = list(state.tags) if state is not None else []
        updated = [t for t in current if t not in remove]
        updated.extend(t for t in add if t not in updated)
        if state is None:
            session.add(ItemState(user_id=user_id, item_id=item_id, tags=updated, updated_at=now))
        elif updated != current:
            state.tags = updated
            state.updated_at = now
    await session.flush()


async def mark_stream_read(
    session: AsyncSession, user_id: uuid.UUID, stream: Stream, *, before: datetime | None = None
) -> int:
    """Mark every unread item in the stream (optionally published at/before `before`) as read."""
    where = await stream_filter(session, user_id, stream)
    if where is None:
        return 0
    stmt = visible_items(user_id).with_only_columns(Item.id).where(where, is_unread_expr())
    if before is not None:
        stmt = stmt.where(Item.published_at <= before)
    ids = list(await session.scalars(stmt))
    await set_read(session, user_id, ids, True)
    return len(ids)


__all__ = [
    "AI_LABEL_PREFIX",
    "BAD_TOKEN_HEADERS",
    "INT63_MASK",
    "ITEM_TAG_PREFIX",
    "LABEL_PREFIX",
    "STREAM_KEPT_UNREAD",
    "STREAM_READ",
    "STREAM_READING_LIST",
    "STREAM_STARRED",
    "DbSession",
    "FeedUnread",
    "ReaderUser",
    "Stream",
    "StreamPage",
    "ai_tags_for",
    "edit_tags",
    "feed_by_ref",
    "feed_stream_id",
    "fetch_stream",
    "folder_by_name",
    "folder_map",
    "from_usec",
    "get_or_create_folder",
    "greader_user",
    "item_hex_id",
    "item_long_id",
    "item_tag_id",
    "items_by_long_ids",
    "label_stream_id",
    "long_id",
    "long_id_sql",
    "mark_stream_read",
    "parse_item_id",
    "parse_stream",
    "parse_timestamp",
    "read_params",
    "set_read",
    "set_starred",
    "stream_filter",
    "to_msec",
    "to_sec",
    "to_usec",
    "unread_by_feed",
    "visible_items",
]
