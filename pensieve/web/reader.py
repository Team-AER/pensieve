"""Three-pane reader: views, list partials with infinite scroll, nav counts, mark-all-read with undo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from pensieve.models import Cluster, ClusterItem, Feed, Folder, Item, ItemAI, ItemState, Tag, User
from pensieve.web.cursor import decode_cursor, encode_cursor
from pensieve.web.queries import (
    NavCounts,
    get_cached_nav_counts,
    is_unread,
    mark_read_where,
    not_hidden,
    parse_uuid,
    set_cached_nav_counts,
    undo_read,
)
from pensieve.web.templating import DB, CsrfUser, CurrentUser, hx_trigger, is_htmx, render
from pensieve.web.undo import load_undo, save_undo

router = APIRouter()

PAGE_SIZE = 40
VIEW_KINDS = {"all", "unread", "starred", "folder", "feed", "tag"}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass
class View:
    kind: str
    key: str | None = None
    title: str = ""
    folder_id: uuid.UUID | None = None
    feed_id: uuid.UUID | None = None
    tag: str | None = None

    @property
    def path(self) -> str:
        return f"/reader/{self.kind}" + (f"/{self.key}" if self.key else "")

    @property
    def name(self) -> str:
        return self.kind + (f"/{self.key}" if self.key else "")


@dataclass
class ListOptions:
    grouped: bool = True
    sort: str = "newest"
    after: str | None = None
    """Keyset cursor (``<published_usec>.<id hex>``) of the last row of the previous page; None = first page."""

    @property
    def query(self) -> str:
        return f"grouped={1 if self.grouped else 0}&sort={self.sort}"

    @property
    def first_page(self) -> bool:
        return not self.after


@dataclass
class Row:
    item: Item
    feed: Feed
    is_read: bool
    is_starred: bool
    cluster_id: uuid.UUID | None = None
    source_count: int = 1
    favicons: list[str | None] = field(default_factory=list)


@dataclass
class FolderNav:
    folder: Folder | None
    feeds: list[tuple[Feed, int]]
    count: int


@dataclass
class NavData:
    view: View
    total_unread: int
    starred: int
    folders: list[FolderNav]
    inbox: list[tuple[Feed, int]]
    inbox_count: int
    user_tags: list[tuple[str, int]]
    ai_tags: list[tuple[str, int]]


def list_options(request: Request) -> ListOptions:
    params = request.query_params
    grouped_raw = params.get("grouped")
    grouped = True if grouped_raw is None else grouped_raw in {"1", "true", "on"}
    sort = params.get("sort", "newest")
    if sort not in {"newest", "oldest"}:
        sort = "newest"
    after = params.get("after") or None
    if after and decode_cursor(after) is None:
        after = None
    return ListOptions(grouped=grouped, sort=sort, after=after)


async def resolve_view(session: AsyncSession, user: User, kind: str, key: str | None) -> View:
    if kind not in VIEW_KINDS:
        raise HTTPException(status_code=404, detail="Unknown view")
    if kind == "all":
        return View("all", title="All items")
    if kind == "unread":
        return View("unread", title="All unread")
    if kind == "starred":
        return View("starred", title="Starred")
    if kind == "folder":
        folder_id = parse_uuid(key)
        folder = await session.get(Folder, folder_id) if folder_id else None
        if folder is None or folder.user_id != user.id:
            raise HTTPException(status_code=404, detail="Folder not found")
        return View("folder", key=str(folder.id), title=folder.name, folder_id=folder.id)
    if kind == "feed":
        feed_id = parse_uuid(key)
        feed = await session.get(Feed, feed_id) if feed_id else None
        if feed is None or feed.user_id != user.id:
            raise HTTPException(status_code=404, detail="Feed not found")
        return View("feed", key=str(feed.id), title=feed.title or feed.url, feed_id=feed.id)
    if not key:
        raise HTTPException(status_code=404, detail="Tag not found")
    return View("tag", key=key, title=f"#{key}", tag=key)


def cluster_subquery(user_id: uuid.UUID):
    return (
        select(
            ClusterItem.item_id.label("item_id"),
            ClusterItem.cluster_id.label("cluster_id"),
            Cluster.canonical_item_id.label("canonical_item_id"),
            Cluster.source_count.label("source_count"),
        )
        .join(Cluster, Cluster.id == ClusterItem.cluster_id)
        .where(Cluster.user_id == user_id)
        .subquery("uc")
    )


def apply_view_filter(stmt, view: View, user_id: uuid.UUID, state):
    if view.kind == "unread":
        stmt = stmt.where(is_unread(state))
    elif view.kind == "starred":
        stmt = stmt.where(state.is_starred.is_(True))
    elif view.kind == "folder":
        stmt = stmt.where(Feed.folder_id == view.folder_id)
    elif view.kind == "feed":
        stmt = stmt.where(Feed.id == view.feed_id)
    elif view.kind == "tag":
        ai_has = exists().where(
            ItemAI.item_id == Item.id, ItemAI.user_id == user_id, ItemAI.tags.any(view.tag)
        )
        stmt = stmt.where(or_(state.tags.any(view.tag), ai_has))
    return stmt


def _filtered_stmt(view: View, user_id: uuid.UUID, columns, state, uc):
    stmt = (
        select(*columns)
        .select_from(Item)
        .join(Feed, Item.feed_id == Feed.id)
        .outerjoin(state, and_(state.item_id == Item.id, state.user_id == user_id))
        .outerjoin(uc, uc.c.item_id == Item.id)
        .where(Feed.user_id == user_id)
        .where(not_hidden(state))
    )
    return apply_view_filter(stmt, view, user_id, state)


def view_items_stmt(view: View, user_id: uuid.UUID, grouped: bool):
    """Base select of (Item, Feed, ItemState, cluster_id, source_count) for a view, user-scoped.

    Grouped: one row per cluster, represented by the cluster's canonical item when the view's filter keeps it,
    otherwise by the newest member that passes the filter (so an unread member still shows after the canonical
    was read, and a feed view shows its own member of a cross-feed story).
    """
    state = aliased(ItemState)
    uc = cluster_subquery(user_id)
    stmt = _filtered_stmt(view, user_id, [Item, Feed, state, uc.c.cluster_id, uc.c.source_count], state, uc)
    if grouped:
        representatives = (
            _filtered_stmt(view, user_id, [Item.id], state, uc)
            .where(uc.c.cluster_id.is_not(None))
            .distinct(uc.c.cluster_id)
            .order_by(
                uc.c.cluster_id,
                (uc.c.canonical_item_id == Item.id).desc(),
                Item.published_at.desc(),
                Item.id.desc(),
            )
            .correlate(None)
        )
        stmt = stmt.where(or_(uc.c.cluster_id.is_(None), Item.id.in_(representatives)))
    return stmt


async def list_rows(
    session: AsyncSession, user: User, view: View, opts: ListOptions
) -> tuple[list[Row], bool]:
    """One page of rows after the keyset cursor in ``opts.after``; stable when items arrive between pages."""
    stmt = view_items_stmt(view, user.id, opts.grouped)
    key = tuple_(Item.published_at, Item.id)
    cursor = decode_cursor(opts.after)
    if opts.sort == "newest":
        stmt = stmt.order_by(Item.published_at.desc(), Item.id.desc())
        if cursor is not None:
            stmt = stmt.where(key < cursor)
    else:
        stmt = stmt.order_by(Item.published_at.asc(), Item.id.asc())
        if cursor is not None:
            stmt = stmt.where(key > cursor)
    stmt = stmt.limit(PAGE_SIZE + 1)
    result = (await session.execute(stmt)).all()
    has_more = len(result) > PAGE_SIZE
    rows: list[Row] = []
    for item, feed, state, cluster_id, source_count in result[:PAGE_SIZE]:
        rows.append(
            Row(
                item=item,
                feed=feed,
                is_read=bool(state and state.is_read),
                is_starred=bool(state and state.is_starred),
                cluster_id=cluster_id if opts.grouped else None,
                source_count=int(source_count or 1) if opts.grouped else 1,
            )
        )
    cluster_ids = [r.cluster_id for r in rows if r.cluster_id]
    if cluster_ids:
        fav = await session.execute(
            select(ClusterItem.cluster_id, Feed.icon_url)
            .join(Item, Item.id == ClusterItem.item_id)
            .join(Feed, Feed.id == Item.feed_id)
            .where(ClusterItem.cluster_id.in_(cluster_ids), Feed.user_id == user.id)
        )
        icons: dict[uuid.UUID, list[str | None]] = {}
        for cid, icon in fav:
            icons.setdefault(cid, []).append(icon)
        for r in rows:
            if r.cluster_id:
                r.favicons = icons.get(r.cluster_id, [])[:4]
    return rows, has_more


async def view_counts(session: AsyncSession, user: User, view: View) -> tuple[int, int]:
    """(unread, total) for the current view."""
    state = aliased(ItemState)
    base = (
        select(func.count(Item.id), func.count(Item.id).filter(is_unread(state)))
        .select_from(Item)
        .join(Feed, Item.feed_id == Feed.id)
        .outerjoin(state, and_(state.item_id == Item.id, state.user_id == user.id))
        .where(Feed.user_id == user.id, not_hidden(state))
    )
    # The unread view counts every item in scope as "total" so the header reads "3 unread · 40 total".
    scope = View("all", title=view.title) if view.kind == "unread" else view
    base = apply_view_filter(base, scope, user.id, state)
    total, unread = (await session.execute(base)).one()
    return int(unread or 0), int(total or 0)


async def _nav_counts(session: AsyncSession, user: User) -> NavCounts:
    """Unread per feed, starred total and per-tag unread counts; cached ``NAV_CACHE_TTL_S`` per user."""
    cached = get_cached_nav_counts(user.id)
    if cached is not None:
        return cached
    state = aliased(ItemState)
    unread_join = (
        select(Feed.id, func.count(Item.id))
        .select_from(Item)
        .join(Feed, Item.feed_id == Feed.id)
        .outerjoin(state, and_(state.item_id == Item.id, state.user_id == user.id))
        .where(Feed.user_id == user.id, not_hidden(state), is_unread(state))
        .group_by(Feed.id)
    )
    unread_by_feed = {fid: int(n) for fid, n in await session.execute(unread_join)}
    starred = await session.scalar(
        select(func.count(ItemState.item_id)).where(
            ItemState.user_id == user.id, ItemState.is_starred.is_(True), ItemState.hidden.is_(False)
        )
    )
    # Tag counts (unread items per tag), for user tags on item_states and AI tags on item_ai.
    user_sq = (
        select(func.unnest(ItemState.tags).label("tag"))
        .select_from(ItemState)
        .where(ItemState.user_id == user.id, ItemState.hidden.is_(False), ItemState.is_read.is_(False))
        .subquery()
    )
    user_tag_counts = {
        t: int(n)
        for t, n in await session.execute(select(user_sq.c.tag, func.count()).group_by(user_sq.c.tag))
    }
    ai_sq = (
        select(func.unnest(ItemAI.tags).label("tag"))
        .select_from(ItemAI)
        .outerjoin(state, and_(state.item_id == ItemAI.item_id, state.user_id == user.id))
        .where(ItemAI.user_id == user.id, not_hidden(state), is_unread(state))
        .subquery()
    )
    ai_tag_counts = {
        t: int(n) for t, n in await session.execute(select(ai_sq.c.tag, func.count()).group_by(ai_sq.c.tag))
    }
    counts = NavCounts(
        unread_by_feed=unread_by_feed,
        starred=int(starred or 0),
        user_tag_counts=user_tag_counts,
        ai_tag_counts=ai_tag_counts,
    )
    set_cached_nav_counts(user.id, counts)
    return counts


async def nav_data(session: AsyncSession, user: User, view: View) -> NavData:
    counts = await _nav_counts(session, user)
    unread_by_feed = counts.unread_by_feed
    starred = counts.starred
    user_tag_counts = counts.user_tag_counts
    ai_tag_counts = counts.ai_tag_counts
    feeds = list(
        await session.scalars(select(Feed).where(Feed.user_id == user.id).order_by(Feed.position, Feed.title))
    )
    folders = list(
        await session.scalars(
            select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
        )
    )
    by_folder: dict[uuid.UUID | None, list[tuple[Feed, int]]] = {}
    for f in feeds:
        by_folder.setdefault(f.folder_id, []).append((f, unread_by_feed.get(f.id, 0)))
    folder_navs = [
        FolderNav(folder=fo, feeds=by_folder.get(fo.id, []), count=sum(n for _, n in by_folder.get(fo.id, [])))
        for fo in folders
    ]
    inbox = by_folder.get(None, [])
    tags = list(await session.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.position, Tag.name)))
    user_tags = sorted({t.name for t in tags if t.kind == "user"} | set(user_tag_counts))
    ai_tags = sorted({t.name for t in tags if t.kind == "ai"} | set(ai_tag_counts))
    return NavData(
        view=view,
        total_unread=sum(unread_by_feed.values()),
        starred=int(starred or 0),
        folders=folder_navs,
        inbox=inbox,
        inbox_count=sum(n for _, n in inbox),
        user_tags=[(t, user_tag_counts.get(t, 0)) for t in user_tags],
        ai_tags=[(t, ai_tag_counts.get(t, 0)) for t in ai_tags],
    )


async def mark_view_read(
    session: AsyncSession, user: User, view: View, older_than: str | None
) -> list[uuid.UUID]:
    """Mark the view's unread items read in one server-side statement; returns the affected ids."""
    state = aliased(ItemState)
    stmt = (
        select(Item.id)
        .join(Feed, Item.feed_id == Feed.id)
        .outerjoin(state, and_(state.item_id == Item.id, state.user_id == user.id))
        .where(Feed.user_id == user.id, not_hidden(state), is_unread(state))
    )
    stmt = apply_view_filter(stmt, view, user.id, state)
    delta = {"1d": timedelta(days=1), "1w": timedelta(weeks=1)}.get(older_than or "")
    if delta:
        stmt = stmt.where(Item.published_at < datetime.now(UTC) - delta)
    return await mark_read_where(session, user.id, stmt)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def render_reader(
    request: Request, session: AsyncSession, user: User, view: View, *, article_html: str | None = None,
):
    opts = list_options(request)
    rows, has_more = await list_rows(session, user, view, opts)
    unread, total = await view_counts(session, user, view)
    nav = await nav_data(session, user, view)
    ctx = {
        "view": view,
        "opts": opts,
        "rows": rows,
        "has_more": has_more,
        "next_cursor": encode_cursor(rows[-1].item) if rows and has_more else None,
        "unread": unread,
        "total": total,
        "nav": nav,
        "article_html": article_html,
        "list_url": f"{view.path}/list",
        "pane": "article" if article_html else "list",
    }
    return render(request, "reader.html", ctx, partial="partials/list.html", user=user)


@router.get("/")
async def home(request: Request, user: CurrentUser, session: DB):
    view = await resolve_view(session, user, "unread", None)
    return await render_reader(request, session, user, view)


@router.get("/reader/nav")
async def nav_partial(
    request: Request,
    user: CurrentUser,
    session: DB,
    view: str = "unread",
):
    kind, _, key = view.partition("/")
    try:
        v = await resolve_view(session, user, kind or "unread", key or None)
    except HTTPException:
        v = View("unread", title="All unread")
    nav = await nav_data(session, user, v)
    return render(request, "partials/nav.html", {"nav": nav, "view": v}, user=user)


@router.get("/reader/{kind}")
async def reader_simple(
    request: Request, kind: str, user: CurrentUser, session: DB,
):
    if kind in {"folder", "feed", "tag"}:
        raise HTTPException(status_code=404)
    view = await resolve_view(session, user, kind, None)
    return await render_reader(request, session, user, view)


@router.get("/reader/{kind}/list")
async def reader_simple_list(
    request: Request, kind: str, user: CurrentUser, session: DB,
):
    view = await resolve_view(session, user, kind, None)
    return await render_list(request, session, user, view)


@router.get("/reader/{kind}/{key}")
async def reader_keyed(
    request: Request,
    kind: str,
    key: str,
    user: CurrentUser,
    session: DB,
):
    if kind not in {"folder", "feed", "tag"}:
        raise HTTPException(status_code=404)
    view = await resolve_view(session, user, kind, key)
    return await render_reader(request, session, user, view)


@router.get("/reader/{kind}/{key}/list")
async def reader_keyed_list(
    request: Request,
    kind: str,
    key: str,
    user: CurrentUser,
    session: DB,
):
    view = await resolve_view(session, user, kind, key)
    return await render_list(request, session, user, view)


async def render_list(request: Request, session: AsyncSession, user: User, view: View, extra: dict | None = None):
    opts = list_options(request)
    rows, has_more = await list_rows(session, user, view, opts)
    ctx = {
        "view": view,
        "opts": opts,
        "rows": rows,
        "has_more": has_more,
        "next_cursor": encode_cursor(rows[-1].item) if rows and has_more else None,
        "list_url": f"{view.path}/list",
    }
    if not opts.first_page and is_htmx(request):
        return render(request, "partials/rows.html", ctx, user=user)
    unread, total = await view_counts(session, user, view)
    ctx.update({"unread": unread, "total": total})
    if extra:
        ctx.update(extra)
    if not is_htmx(request):
        nav = await nav_data(session, user, view)
        ctx.update({"nav": nav, "article_html": None, "pane": "list"})
        return render(request, "reader.html", ctx, user=user)
    return render(request, "partials/list.html", ctx, user=user)


async def _mark_read_common(request, session, user, view: View, older_than: str | None):
    ids = await mark_view_read(session, user, view, older_than)
    await session.commit()
    token = await save_undo(user.id, ids)
    extra = {"undo_token": token, "undo_count": len(ids), "undo_marked": True}
    headers = hx_trigger("counts-changed")
    response = await render_list(request, session, user, view, extra=extra)
    if not is_htmx(request):
        return RedirectResponse(view.path, status_code=303, headers=headers)
    for k, v in headers.items():
        response.headers[k] = v
    return response


@router.post("/reader/{kind}/mark-read")
async def mark_read_simple(
    request: Request,
    kind: str,
    session: DB,
    user: CsrfUser,
    older_than: Annotated[str | None, Form()] = None,
):
    view = await resolve_view(session, user, kind, None)
    return await _mark_read_common(request, session, user, view, older_than)


@router.post("/reader/{kind}/{key}/mark-read")
async def mark_read_keyed(
    request: Request,
    kind: str,
    key: str,
    session: DB,
    user: CsrfUser,
    older_than: Annotated[str | None, Form()] = None,
):
    view = await resolve_view(session, user, kind, key)
    return await _mark_read_common(request, session, user, view, older_than)


@router.post("/items/undo-read")
async def undo_read_route(
    request: Request,
    session: DB,
    user: CsrfUser,
    token: Annotated[str, Form()] = "",
    ids: Annotated[str, Form()] = "",
    view: Annotated[str, Form()] = "unread",
):
    """Undo a mark-all-read batch by token (ids are looked up in the undo store, never trusted from the form).
    A raw ``ids`` list is still accepted for offline replays, capped at the same batch size."""
    item_ids = await load_undo(user.id, token)
    if not item_ids and ids:
        from pensieve.config import get_settings

        item_ids = [u for u in (parse_uuid(p) for p in ids.split(",")) if u][: get_settings().undo_batch_max_ids]
    await undo_read(session, user.id, item_ids)
    await session.commit()
    kind, _, key = view.partition("/")
    v = await resolve_view(session, user, kind or "unread", key or None)
    headers = hx_trigger("counts-changed")
    if not is_htmx(request):
        return RedirectResponse(v.path, status_code=303, headers=headers)
    response = await render_list(request, session, user, v)
    for k, val in headers.items():
        response.headers[k] = val
    return response
