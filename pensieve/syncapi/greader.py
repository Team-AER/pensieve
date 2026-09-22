"""Google Reader API, as spoken by Reeder, NetNewsWire and Unread (FreshRSS / Miniflux dialect).

Mounted twice: at the legacy root paths (`/accounts/ClientLogin`, `/reader/api/0/...`) and under
`/api/greader`. Clients authenticate with `Authorization: GoogleLogin auth=<token>` where the token is an
ApiToken of kind `greader`; ClientLogin also accepts the account password and mints a token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.auth import hash_api_token, user_from_api_token, verify_password
from pensieve.config import get_settings
from pensieve.models import FEED_KIND_SAVED, ApiToken, Feed, Folder, Item, ItemAI, ItemState, Tag, User
from pensieve.syncapi import router
from pensieve.syncapi.common import (
    AI_LABEL_PREFIX,
    LABEL_PREFIX,
    STREAM_READING_LIST,
    STREAM_STARRED,
    DbSession,
    ReaderUser,
    Stream,
    StreamPage,
    ai_tags_for,
    edit_tags,
    feed_by_ref,
    feed_stream_id,
    fetch_stream,
    folder_by_name,
    folder_map,
    get_or_create_folder,
    item_long_id,
    item_tag_id,
    items_by_long_ids,
    label_stream_id,
    long_id,
    mark_stream_read,
    parse_item_id,
    parse_stream,
    parse_timestamp,
    read_params,
    set_read,
    set_starred,
    to_msec,
    to_sec,
    to_usec,
    unread_by_feed,
    visible_items,
)

API = "/reader/api/0"
MAX_CONTENT_ITEMS = 1000
MAX_ID_ITEMS = 10_000
DEFAULT_N = 20
PASSWORD_TOKEN_LABEL = "Google Reader client"
CLIENT_TOKEN_REUSE = timedelta(days=30)

log = logging.getLogger(__name__)
greader_router = APIRouter(tags=["greader"])


def _ok() -> PlainTextResponse:
    return PlainTextResponse("OK")


def _json(payload: dict) -> JSONResponse:
    return JSONResponse(payload)


def _action_token_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="greader-action-token")


def _int_param(params, key: str, default: int, maximum: int) -> int:
    try:
        value = int(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _feed_error_types() -> tuple[type[BaseException], ...]:
    """The fetch package owns FeedError; locate it without a hard import-time dependency."""
    found: list[type[BaseException]] = []
    for module_name in ("pensieve.fetch.subscribe", "pensieve.fetch"):
        try:
            module = __import__(module_name, fromlist=["FeedError"])
        except ImportError:
            continue
        err = getattr(module, "FeedError", None)
        if isinstance(err, type) and issubclass(err, BaseException):
            found.append(err)
    return tuple(found)


async def _add_feed(session: AsyncSession, user: User, url: str, folder_id: uuid.UUID | None) -> Feed:
    try:
        from pensieve.fetch.subscribe import add_feed
    except ImportError as exc:  # fetch package not available
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Feed subscription unavailable"
        ) from exc
    try:
        return await add_feed(session, user, url, folder_id)
    except _feed_error_types() as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _derived_client_token(token_id: uuid.UUID) -> str:
    """Plaintext of a ClientLogin-minted token, reproducible from its row id and the server secret.

    Only the SHA-256 of a token is stored, so a password login could otherwise never hand back the same
    token twice; deriving it with HMAC(secret_key, row id) keeps one token per client per 30 days.
    """
    digest = hmac.new(get_settings().secret_key.encode(), f"clientlogin:{token_id}".encode(), hashlib.sha256)
    return digest.hexdigest()[:40]


async def _reusable_client_token(session: AsyncSession, user: User) -> str | None:
    """An unrevoked ClientLogin token for `user` created in the last 30 days whose plaintext we can derive."""
    since = datetime.now(UTC) - CLIENT_TOKEN_REUSE
    rows = await session.scalars(
        select(ApiToken)
        .where(
            ApiToken.user_id == user.id,
            ApiToken.kind == "greader",
            ApiToken.label == PASSWORD_TOKEN_LABEL,
            ApiToken.revoked_at.is_(None),
            ApiToken.created_at >= since,
        )
        .order_by(ApiToken.created_at.desc())
    )
    for row in rows:
        candidate = _derived_client_token(row.id)
        if hmac.compare_digest(hash_api_token(candidate), row.token_hash):
            row.last_used_at = datetime.now(UTC)
            return candidate
    return None


async def _mint_client_token(session: AsyncSession, user: User) -> str:
    row = ApiToken(
        id=uuid.uuid4(),
        user_id=user.id,
        label=PASSWORD_TOKEN_LABEL,
        token_hash="pending",
        kind="greader",
        last_used_at=datetime.now(UTC),
    )
    token = _derived_client_token(row.id)
    row.token_hash = hash_api_token(token)
    session.add(row)
    await session.flush()
    return token


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _client_login(request: Request, session: AsyncSession) -> Response:
    params = await read_params(request)
    email = (params.get("Email") or params.get("email") or "").strip()
    secret = params.get("Passwd") or params.get("passwd") or ""
    if not email or not secret:
        return PlainTextResponse("Error=BadAuthentication\n", status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        from pensieve.web.ratelimit import get_limiter
    except ImportError:  # web package absent
        limiter = None
    else:
        limiter = get_limiter()
    key = (_client_ip(request), email.lower())
    if limiter is not None:
        retry_after = await limiter.blocked_for(key)
        if retry_after:
            return PlainTextResponse(
                "Error=BadAuthentication\n",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
            )

    user = await user_from_api_token(secret, session)
    token = secret
    if user is None or user.email.lower() != email.lower():
        user = await session.scalar(select(User).where(func.lower(User.email) == email.lower()))
        if user is None or not verify_password(secret, user.password_hash):
            if limiter is not None:
                await limiter.record_failure(key)
            return PlainTextResponse("Error=BadAuthentication\n", status_code=status.HTTP_401_UNAUTHORIZED)
        token = await _reusable_client_token(session, user) or await _mint_client_token(session, user)
    if limiter is not None:
        await limiter.reset(key)
    return PlainTextResponse(f"SID={token}\nLSID=null\nAuth={token}\n")


@greader_router.post("/accounts/ClientLogin")
async def client_login(request: Request, session: DbSession) -> Response:
    """POST only: credentials in a query string end up in access logs and browser history."""
    return await _client_login(request, session)


@greader_router.get(f"{API}/token")
async def action_token(user: ReaderUser) -> Response:
    return PlainTextResponse(_action_token_serializer().dumps(str(user.id)))


@greader_router.get(f"{API}/user-info")
async def user_info(user: ReaderUser) -> Response:
    return _json(
        {
            "userId": str(user.id),
            "userName": user.display_name or user.email.split("@", 1)[0],
            "userProfileId": str(user.id),
            "userEmail": user.email,
        }
    )


@greader_router.get(f"{API}/preference/list")
async def preference_list(user: ReaderUser) -> Response:
    return _json({"prefs": [{"id": "lhn-prefs", "value": '{"subscriptions":{"ssa":"true"}}'}]})


@greader_router.get(f"{API}/preference/stream/list")
async def preference_stream_list(user: ReaderUser) -> Response:
    return _json({"streamprefs": {}})


@greader_router.options(f"{API}/{{rest:path}}")
async def options_any(rest: str) -> Response:
    return Response(status_code=204, headers={"Allow": "GET, POST, HEAD, OPTIONS"})


# ---------------------------------------------------------------------------
# Subscriptions and tags
# ---------------------------------------------------------------------------


def _sortid(feed_id: uuid.UUID) -> str:
    """8-hex sort key, as Google Reader emitted; derived from the feed id so it is stable."""
    return f"{long_id(feed_id) & 0xFFFFFFFF:08x}"


@greader_router.get(f"{API}/subscription/list")
async def subscription_list(user: ReaderUser, session: DbSession) -> Response:
    folders = await folder_map(session, user.id)
    feeds = await session.scalars(
        select(Feed).where(Feed.user_id == user.id).order_by(Feed.position, Feed.title, Feed.created_at)
    )
    oldest = dict(
        (
            await session.execute(
                select(Item.feed_id, func.min(Item.published_at))
                .join(Feed, Feed.id == Item.feed_id)
                .where(Feed.user_id == user.id)
                .group_by(Item.feed_id)
            )
        ).all()
    )
    base = str(get_settings().base_url).rstrip("/")
    subscriptions = []
    for feed in feeds:
        folder = folders.get(feed.folder_id) if feed.folder_id else None
        first = oldest.get(feed.id)
        subscriptions.append(
            {
                "id": feed_stream_id(feed.id),
                "title": feed.title or feed.url,
                "categories": (
                    [{"id": label_stream_id(folder.name), "label": folder.name}] if folder is not None else []
                ),
                "sortid": _sortid(feed.id),
                "firstitemmsec": str(to_msec(first)) if first else "0",
                "url": feed.url,
                "htmlUrl": feed.site_url or "",
                "iconUrl": f"{base}/favicons/{feed.id}" if feed.icon_data else (feed.icon_url or ""),
            }
        )
    return _json({"subscriptions": subscriptions})


async def _user_tag_names(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await session.scalars(
        select(func.unnest(ItemState.tags)).where(ItemState.user_id == user_id).distinct()
    )
    return sorted(t for t in rows if t)


async def _ai_tag_names(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """AI vocabulary tags (tags.kind='ai') that appear on at least one item_ai row."""
    rows = await session.scalars(
        select(Tag.name)
        .where(
            Tag.user_id == user_id,
            Tag.kind == "ai",
            exists().where(ItemAI.user_id == user_id, ItemAI.tags.any(Tag.name)),
        )
        .order_by(Tag.position, Tag.name)
    )
    return list(rows)


@greader_router.get(f"{API}/tag/list")
async def tag_list(user: ReaderUser, session: DbSession) -> Response:
    tags: list[dict] = [{"id": STREAM_STARRED}]
    folders = await folder_map(session, user.id)
    folder_names = {f.name for f in folders.values()}
    for folder in folders.values():
        tags.append({"id": label_stream_id(folder.name), "type": "folder"})
    for name in await _user_tag_names(session, user.id):
        if name not in folder_names:
            tags.append({"id": label_stream_id(name), "type": "tag"})
    for name in await _ai_tag_names(session, user.id):
        tags.append({"id": label_stream_id(AI_LABEL_PREFIX + name), "type": "tag"})
    return _json({"tags": tags})


@greader_router.post(f"{API}/subscription/edit")
async def subscription_edit(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    action = (params.get("ac") or "edit").strip()
    refs = [s for s in params.getlist("s") if s]
    title = params.get("t")
    add_label = parse_stream(params.get("a")) if params.get("a") else None
    remove_label = parse_stream(params.get("r")) if params.get("r") else None
    if not refs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing s")

    if action == "subscribe":
        folder_id = None
        if add_label is not None and add_label.kind == "label":
            folder_id = (await get_or_create_folder(session, user.id, add_label.value)).id
        for ref in refs:
            url = ref.removeprefix("feed/")
            feed = await _add_feed(session, user, url, folder_id)
            if title:
                feed.title = title
            if folder_id is not None and feed.folder_id != folder_id:
                feed.folder_id = folder_id
        await session.flush()
        return _ok()

    feeds: list[Feed] = []
    for ref in refs:
        feed = await feed_by_ref(session, user.id, ref)
        if feed is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown feed {ref}")
        feeds.append(feed)

    if action == "unsubscribe":
        for feed in feeds:
            if feed.kind == FEED_KIND_SAVED:
                continue  # unsubscribing from "Saved" in a sync client must never delete the saved links
            await session.delete(feed)
        await session.flush()
        return _ok()

    if action == "edit":
        for feed in feeds:
            if title is not None and title.strip():
                feed.title = title.strip()
            if add_label is not None and add_label.kind == "label":
                feed.folder_id = (await get_or_create_folder(session, user.id, add_label.value)).id
            elif remove_label is not None and remove_label.kind == "label":
                folder = await folder_by_name(session, user.id, remove_label.value)
                if folder is None or feed.folder_id == folder.id:
                    feed.folder_id = None
        await session.flush()
        return _ok()

    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Unknown action {action}")


@greader_router.post(f"{API}/subscription/quickadd")
async def subscription_quickadd(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    query = (params.get("quickadd") or "").strip()
    query = query.removeprefix("feed/")
    if not query:
        return _json({"numResults": 0, "query": query, "error": "Missing quickadd"})
    try:
        feed = await _add_feed(session, user, query, None)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            return _json({"numResults": 0, "query": query, "error": str(exc.detail)})
        raise
    await session.flush()
    return _json(
        {
            "numResults": 1,
            "query": query,
            "streamId": feed_stream_id(feed.id),
            "streamName": feed.title or feed.url,
        }
    )


@greader_router.post(f"{API}/rename-tag")
async def rename_tag(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    source = parse_stream(params.get("s"))
    dest = parse_stream(params.get("dest"))
    if source.kind != "label" or dest.kind != "label":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Expected s and dest labels")
    if source.value == dest.value:
        return _ok()
    folder = await folder_by_name(session, user.id, source.value)
    if folder is not None:
        target = await folder_by_name(session, user.id, dest.value)
        if target is None:
            folder.name = dest.value
        else:  # merge into the existing folder
            await session.execute(
                Feed.__table__.update()
                .where(Feed.user_id == user.id, Feed.folder_id == folder.id)
                .values(folder_id=target.id)
            )
            await session.delete(folder)
        await session.flush()
        return _ok()
    states = await session.scalars(
        select(ItemState).where(ItemState.user_id == user.id, ItemState.tags.any(source.value))
    )
    for state in states:
        renamed = [dest.value if t == source.value else t for t in state.tags]
        state.tags = list(dict.fromkeys(renamed))
    await session.flush()
    return _ok()


@greader_router.post(f"{API}/disable-tag")
async def disable_tag(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    stream = parse_stream(params.get("s") or (LABEL_PREFIX + (params.get("t") or "")))
    if stream.kind != "label":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Expected a label")
    folder = await folder_by_name(session, user.id, stream.value)
    if folder is not None:
        await session.execute(
            Feed.__table__.update()
            .where(Feed.user_id == user.id, Feed.folder_id == folder.id)
            .values(folder_id=None)
        )
        await session.execute(delete(Folder).where(Folder.id == folder.id))
        return _ok()
    states = await session.scalars(
        select(ItemState).where(ItemState.user_id == user.id, ItemState.tags.any(stream.value))
    )
    for state in states:
        state.tags = [t for t in state.tags if t != stream.value]
    await session.flush()
    return _ok()


# ---------------------------------------------------------------------------
# Unread counts
# ---------------------------------------------------------------------------


@greader_router.get(f"{API}/unread-count")
async def unread_count(user: ReaderUser, session: DbSession) -> Response:
    folders = await folder_map(session, user.id)
    per_feed = await unread_by_feed(session, user.id)
    counts: list[dict] = []
    folder_totals: dict[uuid.UUID, tuple[int, datetime | None]] = {}
    total = 0
    newest_all: datetime | None = None

    def newest(a: datetime | None, b: datetime | None) -> datetime | None:
        if a is None:
            return b
        if b is None:
            return a
        return max(a, b)

    for row in per_feed:
        if row.count <= 0:
            continue
        counts.append(
            {
                "id": feed_stream_id(row.feed_id),
                "count": row.count,
                "newestItemTimestampUsec": str(to_usec(row.newest)) if row.newest else "0",
            }
        )
        total += row.count
        newest_all = newest(newest_all, row.newest)
        if row.folder_id is not None:
            n, ts = folder_totals.get(row.folder_id, (0, None))
            folder_totals[row.folder_id] = (n + row.count, newest(ts, row.newest))
    for folder_id, (n, ts) in folder_totals.items():
        folder = folders.get(folder_id)
        if folder is None:
            continue
        counts.append(
            {
                "id": label_stream_id(folder.name),
                "count": n,
                "newestItemTimestampUsec": str(to_usec(ts)) if ts else "0",
            }
        )
    counts.append(
        {
            "id": STREAM_READING_LIST,
            "count": total,
            "newestItemTimestampUsec": str(to_usec(newest_all)) if newest_all else "0",
        }
    )
    return _json({"max": 1000, "unreadcounts": counts})


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


def _paging(params, maximum: int) -> dict:
    return {
        "n": _int_param(params, "n", DEFAULT_N, maximum),
        "oldest_first": params.get("r") == "o",
        "older_than": parse_timestamp(params.get("ot")),
        "newer_than": parse_timestamp(params.get("nt")),
        "exclude": parse_stream(params.get("xt")) if params.get("xt") else None,
        "include": parse_stream(params.get("it")) if params.get("it") else None,
        "continuation": params.get("c") or None,
    }


def _item_json(
    item: Item,
    feed: Feed,
    state: ItemState | None,
    folders: dict[uuid.UUID, Folder],
    ai_tags: list[str],
) -> dict:
    categories = [STREAM_READING_LIST]
    if state is not None and state.is_read:
        categories.append("user/-/state/com.google/read")
    if state is not None and state.is_starred:
        categories.append(STREAM_STARRED)
    folder = folders.get(feed.folder_id) if feed.folder_id else None
    if folder is not None:
        categories.append(label_stream_id(folder.name))
    if state is not None:
        categories.extend(label_stream_id(t) for t in state.tags)
    categories.extend(label_stream_id(AI_LABEL_PREFIX + t) for t in ai_tags)
    payload: dict = {
        "id": item_tag_id(item.id),
        "crawlTimeMsec": str(to_msec(item.fetched_at or item.published_at)),
        "timestampUsec": str(to_usec(item.published_at)),
        "published": to_sec(item.published_at),
        "updated": to_sec(item.updated_at or item.published_at),
        "title": item.title or "",
        "canonical": [{"href": item.url or ""}],
        "alternate": [{"href": item.url or "", "type": "text/html"}],
        "categories": categories,
        "origin": {
            "streamId": feed_stream_id(feed.id),
            "title": feed.title or feed.url,
            "htmlUrl": feed.site_url or "",
        },
        "summary": {"direction": "ltr", "content": item.content_html or ""},
    }
    if item.author:
        payload["author"] = item.author
    if item.enclosure_url:
        payload["enclosure"] = [{"href": item.enclosure_url, "type": item.enclosure_type or ""}]
    return payload


async def _contents_payload(session: AsyncSession, user: User, stream_id: str, page) -> dict:
    folders = await folder_map(session, user.id)
    ai_tags = await ai_tags_for(session, user.id, [item.id for item, _, _ in page.rows])
    payload = {
        "id": stream_id,
        "updated": to_sec(datetime.now(UTC)),
        "items": [
            _item_json(item, feed, state, folders, ai_tags.get(item.id, []))
            for item, feed, state in page.rows
        ],
    }
    if page.continuation:
        payload["continuation"] = page.continuation
    return payload


def _stream_label(stream: Stream) -> str:
    if stream.kind == "feed":
        return f"feed/{stream.value}"
    if stream.kind == "label":
        return label_stream_id(stream.value)
    return {
        "reading-list": STREAM_READING_LIST,
        "starred": STREAM_STARRED,
        "read": "user/-/state/com.google/read",
        "kept-unread": "user/-/state/com.google/kept-unread",
    }.get(stream.kind, stream.value)


@greader_router.get(f"{API}/stream/items/ids")
async def stream_items_ids(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    stream = parse_stream(params.get("s"))
    page = await fetch_stream(session, user.id, stream, **_paging(params, MAX_ID_ITEMS))
    refs = [
        {
            "id": str(item_long_id(item.id)),
            "directStreamIds": [feed_stream_id(feed.id)],
            "timestampUsec": str(to_usec(item.published_at)),
        }
        for item, feed, _ in page.rows
    ]
    payload: dict = {"itemRefs": refs}
    if page.continuation:
        payload["continuation"] = page.continuation
    return _json(payload)


@greader_router.get(f"{API}/stream/contents")
@greader_router.get(f"{API}/stream/contents/{{stream_id:path}}")
async def stream_contents(
    request: Request,
    user: ReaderUser,
    session: DbSession,
    stream_id: str = "",
) -> Response:
    params = await read_params(request)
    stream = parse_stream(stream_id or params.get("s"))
    page = await fetch_stream(session, user.id, stream, **_paging(params, MAX_CONTENT_ITEMS))
    return _json(await _contents_payload(session, user, _stream_label(stream), page))


@greader_router.post(f"{API}/stream/items/contents")
@greader_router.get(f"{API}/stream/items/contents")
async def stream_items_contents(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    long_ids = [lid for lid in (parse_item_id(raw) for raw in params.getlist("i")) if lid is not None]
    item_ids = await items_by_long_ids(session, user.id, long_ids)
    rows: list = []
    if item_ids:
        stmt = (
            visible_items(user.id)
            .where(Item.id.in_(item_ids))
            .order_by(Item.published_at.desc(), Item.id.desc())
        )
        rows = [(item, feed, state) for item, feed, state in (await session.execute(stmt)).all()]
    page = StreamPage(rows, None)
    return _json(await _contents_payload(session, user, STREAM_READING_LIST, page))


# ---------------------------------------------------------------------------
# State edits
# ---------------------------------------------------------------------------


@greader_router.post(f"{API}/edit-tag")
async def edit_tag(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    long_ids = [lid for lid in (parse_item_id(raw) for raw in params.getlist("i")) if lid is not None]
    item_ids = await items_by_long_ids(session, user.id, long_ids)
    adds = [parse_stream(a) for a in params.getlist("a") if a]
    removes = [parse_stream(r) for r in params.getlist("r") if r]

    add_tags: list[str] = []
    remove_tags: list[str] = []
    for tag in adds:
        if tag.kind == "read":
            await set_read(session, user.id, item_ids, True)
        elif tag.kind == "kept-unread":
            await set_read(session, user.id, item_ids, False)
        elif tag.kind == "starred":
            await set_starred(session, user.id, item_ids, True)
            from pensieve.archive.save import archive_after_star

            await archive_after_star(session, user.id, item_ids)
        elif tag.kind == "label":
            add_tags.append(tag.value)
    for tag in removes:
        if tag.kind == "read":
            await set_read(session, user.id, item_ids, False)
        elif tag.kind == "kept-unread":
            await set_read(session, user.id, item_ids, True)
        elif tag.kind == "starred":
            await set_starred(session, user.id, item_ids, False)
        elif tag.kind == "label":
            remove_tags.append(tag.value)
    if add_tags or remove_tags:
        await edit_tags(session, user.id, item_ids, add_tags, remove_tags)
    return _ok()


@greader_router.post(f"{API}/mark-all-as-read")
async def mark_all_as_read(request: Request, user: ReaderUser, session: DbSession) -> Response:
    params = await read_params(request)
    stream = parse_stream(params.get("s"))
    before = parse_timestamp(params.get("ts"))
    await mark_stream_read(session, user.id, stream, before=before)
    return _ok()


# Mount once at the legacy root and once under /api/greader.
router.include_router(greader_router)
router.include_router(greader_router, prefix="/api/greader")
