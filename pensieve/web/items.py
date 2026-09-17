"""Article pane and per-item state endpoints."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from pensieve import queue
from pensieve.models import Cluster, ClusterItem, Feed, Item, ItemAI, ItemState, Note, User
from pensieve.web.queries import get_state, get_user_item, set_read, set_starred, upsert_states
from pensieve.web.templating import DB, CsrfUser, CurrentUser, hx_trigger, render

log = logging.getLogger(__name__)
router = APIRouter()


async def load_item(session: AsyncSession, user: User, item_id: uuid.UUID) -> tuple[Item, Feed]:
    pair = await get_user_item(session, user, item_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return pair


async def article_context(session: AsyncSession, user: User, item: Item, feed: Feed) -> dict:
    state = await get_state(session, user, item.id)
    ai = await session.get(ItemAI, (user.id, item.id))
    notes = list(
        await session.scalars(
            select(Note).where(Note.user_id == user.id, Note.item_id == item.id).order_by(Note.created_at)
        )
    )
    cluster = (
        await session.execute(
            select(Cluster)
            .join(ClusterItem, ClusterItem.cluster_id == Cluster.id)
            .where(ClusterItem.item_id == item.id, Cluster.user_id == user.id)
        )
    ).scalar_one_or_none()
    related: list = []
    try:
        from pensieve.ai.service import related_history  # type: ignore[import-not-found]

        related = list(await related_history(session, user, item, limit=5) or [])
    except Exception:  # noqa: BLE001  AI is additive; never let it break the reader
        related = []
    ai_tags = []
    if ai:
        for name in ai.tags or []:
            ai_tags.append((name, float((ai.confidences or {}).get(name, 0) or 0)))
    return {
        "item": item,
        "feed": feed,
        "state": state,
        "is_read": bool(state and state.is_read),
        "is_starred": bool(state and state.is_starred),
        "user_tags": list(state.tags) if state and state.tags else [],
        "ai": ai,
        "ai_tags": ai_tags,
        "notes": notes,
        "cluster": cluster,
        "related": related,
        "use_reader": False,
    }


def state_headers(item: Item, is_read: bool | None = None, is_starred: bool | None = None) -> dict[str, str]:
    detail: dict = {"id": str(item.id)}
    if is_read is not None:
        detail["read"] = is_read
    if is_starred is not None:
        detail["starred"] = is_starred
    return hx_trigger("counts-changed", **{"item-state": detail})


@router.get("/items/{item_id}")
async def article(
    request: Request,
    item_id: uuid.UUID,
    user: CurrentUser,
    session: DB,
    keep_unread: int = 0,
):
    item, feed = await load_item(session, user, item_id)
    marked = False
    if not keep_unread:
        state = await get_state(session, user, item.id)
        if not (state and state.is_read):
            await set_read(session, user.id, [item.id], True)
            await session.commit()
            marked = True
    ctx = await article_context(session, user, item, feed)
    headers = state_headers(item, is_read=True) if marked else None
    if request.headers.get("hx-request") == "true":
        return render(request, "partials/article.html", ctx, user=user, headers=headers)
    # Deep link: render the whole reader with the article open.
    from pensieve.web.reader import render_reader, resolve_view

    view = await resolve_view(session, user, "all", None)
    article_html = render(request, "partials/article.html", ctx, user=user).body.decode()
    return await render_reader(request, session, user, view, article_html=article_html)


@router.get("/items/{item_id}/toolbar")
async def toolbar(
    request: Request,
    item_id: uuid.UUID,
    user: CurrentUser,
    session: DB,
):
    item, feed = await load_item(session, user, item_id)
    ctx = await article_context(session, user, item, feed)
    return render(request, "partials/toolbar.html", ctx, user=user)


async def _toolbar_response(request, session, user, item, feed, headers):
    ctx = await article_context(session, user, item, feed)
    return render(request, "partials/toolbar.html", ctx, user=user, headers=headers)


@router.post("/items/{item_id}/read")
async def mark_read(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    item, feed = await load_item(session, user, item_id)
    await set_read(session, user.id, [item.id], True)
    await session.commit()
    return await _toolbar_response(request, session, user, item, feed, state_headers(item, is_read=True))


@router.post("/items/{item_id}/unread")
async def mark_unread(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    item, feed = await load_item(session, user, item_id)
    await set_read(session, user.id, [item.id], False)
    await session.commit()
    return await _toolbar_response(request, session, user, item, feed, state_headers(item, is_read=False))


@router.post("/items/{item_id}/star")
async def star(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    item, feed = await load_item(session, user, item_id)
    await set_starred(session, user.id, item.id, True)
    await session.commit()
    return await _toolbar_response(request, session, user, item, feed, state_headers(item, is_starred=True))


@router.post("/items/{item_id}/unstar")
async def unstar(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    item, feed = await load_item(session, user, item_id)
    await set_starred(session, user.id, item.id, False)
    await session.commit()
    return await _toolbar_response(request, session, user, item, feed, state_headers(item, is_starred=False))


@router.post("/items/{item_id}/reader-mode")
async def reader_mode(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    off: Annotated[int, Form()] = 0,
):
    item, feed = await load_item(session, user, item_id)
    ctx = await article_context(session, user, item, feed)
    error = None
    if off:
        ctx["use_reader"] = False
    else:
        if not item.reader_html:
            if not item.url:
                error = "This item has no link to extract."
            else:
                try:
                    from pensieve.fetch.reader_mode import (
                        extract_reader_html,  # type: ignore[import-not-found]
                    )

                    html = await extract_reader_html(item.url)
                except ImportError:
                    html = None
                    error = "Reader view is not available yet."
                except Exception as exc:  # noqa: BLE001
                    log.warning("reader mode failed for %s: %s", item.id, exc)
                    html = None
                    error = "Couldn't extract a reader view for this page."
                if html:
                    item.reader_html = html
                    item.reader_fetched_at = datetime.now(UTC)
                    await session.commit()
                elif not error:
                    error = "Nothing readable was found on that page."
        ctx["use_reader"] = bool(item.reader_html) and not error
    ctx["error"] = error
    return render(request, "partials/body.html", ctx, user=user)


@router.post("/items/{item_id}/summarize")
async def summarize(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    item, _feed = await load_item(session, user, item_id)
    ai = await session.get(ItemAI, (user.id, item.id))
    error = None
    if not (ai and ai.summary):
        try:
            await queue.enqueue(
                queue.AI_SUMMARIZE_ITEM,
                str(user.id),
                str(item.id),
                _job_id=queue.job_id_for(queue.AI_SUMMARIZE_ITEM, item.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enqueue summary for %s: %s", item.id, exc)
            error = "The AI queue is unavailable right now."
    ctx = {"item": item, "ai": ai, "pending": not (ai and ai.summary) and not error, "error": error}
    return render(request, "partials/summary.html", ctx, user=user)


@router.get("/items/{item_id}/summary")
async def summary_poll(
    request: Request,
    item_id: uuid.UUID,
    user: CurrentUser,
    session: DB,
):
    item, _feed = await load_item(session, user, item_id)
    ai = await session.get(ItemAI, (user.id, item.id))
    ctx = {"item": item, "ai": ai, "pending": not (ai and ai.summary), "error": None}
    return render(request, "partials/summary.html", ctx, user=user)


@router.post("/items/{item_id}/note")
async def note(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    body: Annotated[str, Form()] = "",
    quote: Annotated[str, Form()] = "",
    note_id: Annotated[str, Form()] = "",
    delete: Annotated[int, Form()] = 0,
):
    item, feed = await load_item(session, user, item_id)
    existing = None
    if note_id:
        try:
            existing = await session.get(Note, uuid.UUID(note_id))
        except ValueError:
            existing = None
        if existing and (existing.user_id != user.id or existing.item_id != item.id):
            existing = None
    if existing is None and not delete:
        existing = await session.scalar(
            select(Note).where(Note.user_id == user.id, Note.item_id == item.id).order_by(Note.created_at.desc())
        )
    if delete:
        if existing:
            await session.delete(existing)
    elif existing:
        existing.body = body.strip()
        existing.quote = quote.strip()
    elif body.strip() or quote.strip():
        session.add(Note(user_id=user.id, item_id=item.id, body=body.strip(), quote=quote.strip()))
    await session.commit()
    ctx = await article_context(session, user, item, feed)
    return render(request, "partials/notes.html", ctx, user=user)


@router.post("/items/{item_id}/tag")
async def tag(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    name: Annotated[str, Form()] = "",
    op: Annotated[str, Form()] = "add",
):
    item, feed = await load_item(session, user, item_id)
    name = name.strip()[:120]
    if name:
        state = await get_state(session, user, item.id)
        tags = list(state.tags) if state and state.tags else []
        if op == "remove":
            tags = [t for t in tags if t != name]
            ai = await session.get(ItemAI, (user.id, item.id))
            if ai and name in (ai.tags or []):
                ai.tags = [t for t in ai.tags if t != name]
                conf = dict(ai.confidences or {})
                conf.pop(name, None)
                ai.confidences = conf
                try:
                    from pensieve.ai.service import record_correction  # type: ignore[import-not-found]

                    await record_correction(session, user, "item_tag", item.id, "tags", name, None)
                except Exception as exc:  # noqa: BLE001
                    log.warning("record_correction unavailable: %s", exc)
        elif name not in tags:
            tags.append(name)
        await upsert_states(session, user.id, [item.id], tags=tags)
        await session.commit()
    ctx = await article_context(session, user, item, feed)
    return render(request, "partials/tags.html", ctx, user=user)


@router.post("/items/{item_id}/unmerge")
async def unmerge(
    request: Request,
    item_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    item, feed = await load_item(session, user, item_id)
    error = None
    try:
        from pensieve.ai.service import unmerge as ai_unmerge  # type: ignore[import-not-found]

        await ai_unmerge(session, user, item.id)
        await session.commit()
    except ImportError:
        error = "Unmerge is not available yet."
    except Exception as exc:  # noqa: BLE001
        log.warning("unmerge failed for %s: %s", item.id, exc)
        error = "Couldn't split this story out of its group."
    ctx = await article_context(session, user, item, feed)
    ctx["error"] = error
    return render(request, "partials/toolbar.html", ctx, user=user, headers=hx_trigger("list-changed"))


@router.get("/clusters/{cluster_id}")
async def cluster_items(
    request: Request,
    cluster_id: uuid.UUID,
    user: CurrentUser,
    session: DB,
):
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None or cluster.user_id != user.id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    state = aliased(ItemState)
    rows = (
        await session.execute(
            select(Item, Feed, state, ClusterItem.similarity)
            .join(ClusterItem, ClusterItem.item_id == Item.id)
            .join(Feed, Feed.id == Item.feed_id)
            .outerjoin(state, and_(state.item_id == Item.id, state.user_id == user.id))
            .where(ClusterItem.cluster_id == cluster.id, Feed.user_id == user.id)
            .order_by(Item.published_at.desc())
        )
    ).all()
    members = [
        {"item": i, "feed": f, "is_read": bool(s and s.is_read), "is_starred": bool(s and s.is_starred), "sim": sim}
        for i, f, s, sim in rows
    ]
    return render(request, "partials/cluster.html", {"cluster": cluster, "members": members}, user=user)


