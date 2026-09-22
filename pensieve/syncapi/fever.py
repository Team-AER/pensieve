"""Fever API (Reeder, Unread, Fiery Feeds, ...), mounted at `/api/fever/` and `/fever/`.

Authentication
--------------
Fever clients send `api_key = md5("<email>:<password>")` in the POST body. Because only that digest ever
reaches the server, an ApiToken of kind `fever` stores `token_hash = md5("<email>:<plaintext>")` (hex)
rather than the SHA-256 the other kinds use. The web UI must create fever tokens through
`fever_api_key_hash(email, plaintext)` so the stored value matches what the client will send.

Ids
---
Fever wants integer ids for feeds, groups and items; all three are `common.long_id(uuid)`.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.models import ApiToken, Feed, Folder, Item, ItemState, User
from pensieve.syncapi import router
from pensieve.syncapi.common import (
    DbSession,
    Stream,
    is_starred_expr,
    is_unread_expr,
    items_by_long_ids,
    long_id,
    long_id_sql,
    mark_stream_read,
    parse_timestamp,
    read_params,
    set_read,
    set_starred,
    to_sec,
    visible_items,
)

API_VERSION = 3
PAGE_SIZE = 50
MAX_STATE_IDS = 20_000
RECENTLY_READ = timedelta(hours=1)

fever_router = APIRouter(tags=["fever"])


def fever_api_key_hash(email: str, plaintext: str) -> str:
    """What a Fever client sends as `api_key`, and what an ApiToken of kind 'fever' stores in token_hash."""
    return hashlib.md5(f"{email}:{plaintext}".encode()).hexdigest()


async def _authenticate(session: AsyncSession, api_key: str | None) -> User | None:
    if not api_key:
        return None
    key = api_key.strip().lower()
    if len(key) != 32:
        return None
    token = await session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == key, ApiToken.kind == "fever", ApiToken.revoked_at.is_(None)
        )
    )
    if token is None:
        return None
    token.last_used_at = datetime.now(UTC)
    return await session.get(User, token.user_id)


async def _last_refreshed(session: AsyncSession, user_id: uuid.UUID) -> int:
    latest = await session.scalar(select(func.max(Feed.last_success_at)).where(Feed.user_id == user_id))
    return to_sec(latest) if latest else to_sec(datetime.now(UTC))


async def _groups(session: AsyncSession, user_id: uuid.UUID) -> tuple[list[dict], list[dict]]:
    folders = list(
        await session.scalars(
            select(Folder).where(Folder.user_id == user_id).order_by(Folder.position, Folder.name)
        )
    )
    feeds = await session.execute(
        select(Feed.id, Feed.folder_id).where(Feed.user_id == user_id).order_by(Feed.position)
    )
    members: dict[uuid.UUID, list[int]] = {}
    for feed_id, folder_id in feeds:
        if folder_id is not None:
            members.setdefault(folder_id, []).append(long_id(feed_id))
    groups = [{"id": long_id(f.id), "title": f.name} for f in folders]
    feeds_groups = [
        {"group_id": long_id(f.id), "feed_ids": ",".join(str(i) for i in members.get(f.id, []))}
        for f in folders
    ]
    return groups, feeds_groups


async def _feeds(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    feeds = await session.scalars(
        select(Feed).where(Feed.user_id == user_id).order_by(Feed.position, Feed.title)
    )
    return [
        {
            "id": long_id(feed.id),
            "favicon_id": long_id(feed.id) if feed.icon_data else 0,
            "title": feed.title or feed.url,
            "url": feed.url,
            "site_url": feed.site_url or "",
            "is_spark": 0,
            "last_updated_on_time": to_sec(feed.last_success_at) if feed.last_success_at else 0,
        }
        for feed in feeds
    ]


def _parse_ids(raw: str | None) -> list[int]:
    ids = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


async def _items(session: AsyncSession, user_id: uuid.UUID, params) -> tuple[list[dict], int]:
    stmt = visible_items(user_id)
    item_lid = long_id_sql(Item.id)
    with_ids = _parse_ids(params.get("with_ids"))
    since_id = params.get("since_id")
    max_id = params.get("max_id")
    if with_ids:
        stmt = stmt.where(item_lid.in_(with_ids)).order_by(item_lid.asc())
    elif since_id and since_id.isdigit():
        stmt = stmt.where(item_lid > int(since_id)).order_by(item_lid.asc())
    elif max_id and max_id.isdigit():
        stmt = stmt.where(item_lid < int(max_id)).order_by(item_lid.desc())
    else:
        stmt = stmt.order_by(item_lid.desc())
    rows = (await session.execute(stmt.limit(PAGE_SIZE))).all()
    total = await session.scalar(visible_items(user_id).with_only_columns(func.count(Item.id)))
    items = [
        {
            "id": long_id(item.id),
            "feed_id": long_id(feed.id),
            "title": item.title or "",
            "author": item.author or "",
            "html": item.content_html or "",
            "url": item.url or "",
            "is_saved": 1 if state is not None and state.is_starred else 0,
            "is_read": 1 if state is not None and state.is_read else 0,
            "created_on_time": to_sec(item.published_at),
        }
        for item, feed, state in rows
    ]
    return items, int(total or 0)


async def _favicons(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    """Cached icon bytes as ``{"id": favicon_id, "data": "<mime>;base64,<...>"}`` (Fever's shape)."""
    rows = await session.execute(
        select(Feed.id, Feed.icon_data, Feed.icon_content_type).where(
            Feed.user_id == user_id, Feed.icon_data.is_not(None)
        )
    )
    return [
        {
            "id": long_id(feed_id),
            "data": f"{content_type or 'image/x-icon'};base64,{base64.b64encode(data).decode('ascii')}",
        }
        for feed_id, data, content_type in rows
    ]


async def _state_ids(session: AsyncSession, user_id: uuid.UUID, clause) -> str:
    """Comma-separated long ids for a state, newest ``MAX_STATE_IDS`` first (Fever clients want a bounded list)."""
    stmt = (
        visible_items(user_id)
        .with_only_columns(Item.id)
        .where(clause)
        .order_by(Item.published_at.desc(), Item.id.desc())
        .limit(MAX_STATE_IDS)
    )
    return ",".join(str(long_id(i)) for i in await session.scalars(stmt))


async def _unread_recently_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Fever ``unread_recently_read=1``: revert reads made in the last hour (an "oops" for mark-all-read)."""
    since = datetime.now(UTC) - RECENTLY_READ
    result = await session.execute(
        update(ItemState)
        .where(ItemState.user_id == user_id, ItemState.is_read.is_(True), ItemState.read_at >= since)
        .values(is_read=False, read_at=None, updated_at=datetime.now(UTC))
    )
    try:
        from pensieve.web.queries import invalidate_nav_cache

        invalidate_nav_cache(user_id)
    except ImportError:
        pass
    return result.rowcount or 0


async def _mark(session: AsyncSession, user_id: uuid.UUID, params) -> None:
    target = params.get("mark")
    action = params.get("as")
    raw_id = params.get("id") or ""
    try:
        target_id = int(raw_id)
    except ValueError:
        return
    before = parse_timestamp(params.get("before"))

    if target == "item":
        item_ids = await items_by_long_ids(session, user_id, [target_id])
        if action == "read":
            await set_read(session, user_id, item_ids, True)
        elif action == "unread":
            await set_read(session, user_id, item_ids, False)
        elif action == "saved":
            await set_starred(session, user_id, item_ids, True)
            from pensieve.archive.save import archive_after_star

            await archive_after_star(session, user_id, item_ids)
        elif action == "unsaved":
            await set_starred(session, user_id, item_ids, False)
        return

    if action != "read":
        return
    if target == "feed":
        feed_id = await session.scalar(
            select(Feed.id).where(Feed.user_id == user_id, long_id_sql(Feed.id) == target_id)
        )
        if feed_id is not None:
            await mark_stream_read(session, user_id, Stream("feed", str(feed_id)), before=before)
    elif target == "group":
        if target_id <= 0:  # 0 = Kindling (everything), -1 = Sparks
            await mark_stream_read(session, user_id, Stream("reading-list"), before=before)
            return
        folder = await session.scalar(
            select(Folder).where(Folder.user_id == user_id, long_id_sql(Folder.id) == target_id)
        )
        if folder is not None:
            await mark_stream_read(session, user_id, Stream("label", folder.name), before=before)


async def _fever(request: Request, session: AsyncSession) -> Response:
    params = await read_params(request)
    user = await _authenticate(session, params.get("api_key"))
    if user is None:
        return JSONResponse({"api_version": API_VERSION, "auth": 0})

    payload: dict = {
        "api_version": API_VERSION,
        "auth": 1,
        "last_refreshed_on_time": await _last_refreshed(session, user.id),
    }
    if "mark" in params:
        await _mark(session, user.id, params)
    if params.get("unread_recently_read") in {"1", "true"}:
        await _unread_recently_read(session, user.id)
    if "groups" in params:
        payload["groups"], payload["feeds_groups"] = await _groups(session, user.id)
    if "feeds" in params:
        payload["feeds"] = await _feeds(session, user.id)
        if "feeds_groups" not in payload:
            _, payload["feeds_groups"] = await _groups(session, user.id)
    if "favicons" in params:
        payload["favicons"] = await _favicons(session, user.id)
    if "items" in params:
        payload["items"], payload["total_items"] = await _items(session, user.id, params)
    if "links" in params:
        payload["links"] = []
    if "unread_item_ids" in params:
        payload["unread_item_ids"] = await _state_ids(session, user.id, is_unread_expr())
    if "saved_item_ids" in params:
        payload["saved_item_ids"] = await _state_ids(session, user.id, is_starred_expr())
    return JSONResponse(payload)


@fever_router.post("/api/fever/")
@fever_router.post("/api/fever")
@fever_router.post("/fever/")
@fever_router.post("/fever")
@fever_router.get("/api/fever/")
@fever_router.get("/api/fever")
@fever_router.get("/fever/")
@fever_router.get("/fever")
async def fever_endpoint(request: Request, session: DbSession) -> Response:
    return await _fever(request, session)


router.include_router(fever_router)

__all__ = ["fever_api_key_hash", "fever_router"]
