"""Full-text search over items (tsvector) and 'Ask my reading'."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased

from pensieve.models import Feed, Folder, Item, ItemState
from pensieve.web.queries import is_unread, not_hidden, parse_uuid
from pensieve.web.reader import PAGE_SIZE, Row, View, nav_data
from pensieve.web.templating import DB, CsrfUser, CurrentUser, is_htmx, render

log = logging.getLogger(__name__)
router = APIRouter()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


@router.get("/search")
async def search(
    request: Request,
    user: CurrentUser,
    session: DB,
    q: str = "",
    feed: str = "",
    folder: str = "",
    state: str = "",
    since: str = "",
    until: str = "",
    page: int = 1,
):
    q = q.strip()
    page = max(1, page)
    st = aliased(ItemState)
    rows: list[Row] = []
    has_more = False
    total = 0
    feed_id = parse_uuid(feed)
    folder_id = parse_uuid(folder)
    if q:
        tsq = func.websearch_to_tsquery("english", q)
        stmt = (
            select(Item, Feed, st)
            .join(Feed, Item.feed_id == Feed.id)
            .outerjoin(st, and_(st.item_id == Item.id, st.user_id == user.id))
            .where(Feed.user_id == user.id, not_hidden(st), Item.search_vector.op("@@")(tsq))
        )
        if feed_id:
            stmt = stmt.where(Feed.id == feed_id)
        if folder_id:
            stmt = stmt.where(Feed.folder_id == folder_id)
        if state == "unread":
            stmt = stmt.where(is_unread(st))
        elif state == "starred":
            stmt = stmt.where(st.is_starred.is_(True))
        if d := _parse_date(since):
            stmt = stmt.where(Item.published_at >= d)
        if d := _parse_date(until):
            stmt = stmt.where(Item.published_at < d + timedelta(days=1))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(await session.scalar(count_stmt) or 0)
        stmt = (
            stmt.order_by(func.ts_rank_cd(Item.search_vector, tsq).desc(), Item.published_at.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE + 1)
        )
        result = (await session.execute(stmt)).all()
        has_more = len(result) > PAGE_SIZE
        rows = [
            Row(item=i, feed=f, is_read=bool(s and s.is_read), is_starred=bool(s and s.is_starred))
            for i, f, s in result[:PAGE_SIZE]
        ]
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id).order_by(Feed.title)))
    folders = list(
        await session.scalars(select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name))
    )
    params = {"q": q, "feed": feed, "folder": folder, "state": state, "since": since, "until": until}
    view = View("search", title="Search")
    ctx = {
        "view": view,
        "q": q,
        "params": params,
        "rows": rows,
        "has_more": has_more,
        "total": total,
        "page": page,
        "feeds": feeds,
        "folders": folders,
        "pane": "list",
        "article_html": None,
    }
    if is_htmx(request) and page > 1:
        return render(request, "partials/search_rows.html", ctx, user=user)
    if is_htmx(request):
        return render(request, "partials/search_list.html", ctx, user=user)
    ctx["nav"] = await nav_data(session, user, view)
    return render(request, "search.html", ctx, user=user)


@router.post("/ask")
async def ask(
    request: Request,
    user: CsrfUser,
    session: DB,
    question: Annotated[str, Form()] = "",
):
    question = question.strip()
    answer = None
    citations: list = []
    error = None
    if not question:
        error = "Ask something about what you've read."
    else:
        try:
            from pensieve.ai.service import ask_reading  # type: ignore[import-not-found]

            result = await ask_reading(session, user, question)
            answer = getattr(result, "text", str(result))
            citations = list(getattr(result, "citations", []) or [])
        except ImportError:
            error = "Ask my reading isn't available yet."
        except Exception as exc:  # noqa: BLE001
            log.warning("ask_reading failed: %s", exc)
            error = "The AI gateway didn't answer. Try again in a moment."
    ctx = {"question": question, "answer": answer, "citations": citations, "error": error}
    return render(request, "partials/ask.html", ctx, user=user)
