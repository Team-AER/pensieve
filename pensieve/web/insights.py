"""Daily digest, weekly review and stored insights."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.models import Insight, User
from pensieve.web.queries import parse_uuid, set_read, user_owns_items
from pensieve.web.templating import DB, CsrfUser, CurrentUser, hx_trigger, render

log = logging.getLogger(__name__)
router = APIRouter()


async def latest(session: AsyncSession, user: User, kind: str) -> Insight | None:
    return await session.scalar(
        select(Insight)
        .where(Insight.user_id == user.id, Insight.kind == kind)
        .order_by(Insight.period.desc(), Insight.generated_at.desc())
        .limit(1)
    )


async def _render_insight(request: Request, session: AsyncSession, user: User, insight: Insight | None, kind: str):
    if insight is not None and insight.opened_at is None:
        insight.opened_at = datetime.now(UTC)
        await session.commit()
    body = dict(insight.body or {}) if insight else {}
    others = list(
        await session.scalars(
            select(Insight)
            .where(Insight.user_id == user.id, Insight.kind.in_(["daily", "weekly"]))
            .order_by(Insight.period.desc())
            .limit(14)
        )
    )
    ctx = {
        "insight": insight,
        "kind": kind,
        "body": body,
        "top_stories": body.get("top_stories") or [],
        "safe_to_skip": body.get("safe_to_skip") or {},
        "starred_unread": body.get("starred_unread") or [],
        "trends": body.get("trends") or [],
        "summary": body.get("summary") or "",
        "history": others,
        "queued": request.query_params.get("queued") == "1",
    }
    return render(request, "insights.html", ctx, partial="partials/insight_body.html", user=user)


@router.get("/insights")
async def daily(request: Request, user: CurrentUser, session: DB):
    return await _render_insight(request, session, user, await latest(session, user, "daily"), "daily")


@router.get("/insights/weekly")
async def weekly(request: Request, user: CurrentUser, session: DB):
    return await _render_insight(request, session, user, await latest(session, user, "weekly"), "weekly")


@router.get("/insights/{insight_id}")
async def by_id(
    request: Request,
    insight_id: uuid.UUID,
    user: CurrentUser,
    session: DB,
):
    insight = await session.get(Insight, insight_id)
    if insight is None or insight.user_id != user.id:
        raise HTTPException(status_code=404, detail="Insight not found")
    return await _render_insight(request, session, user, insight, insight.kind)


@router.post("/insights/generate")
async def generate(
    request: Request,
    user: CsrfUser,
    kind: Annotated[str, Form()] = "daily",
):
    job = queue.AI_WEEKLY_REVIEW if kind == "weekly" else queue.AI_DAILY_DIGEST
    try:
        await queue.enqueue(job, str(user.id), _job_id=queue.job_id_for(job, user.id))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enqueue %s: %s", job, exc)
    target = "/insights/weekly" if kind == "weekly" else "/insights"
    return RedirectResponse(f"{target}?queued=1", status_code=303)


@router.post("/insights/{insight_id}/skip-read")
async def skip_read(
    request: Request,
    insight_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    insight = await session.get(Insight, insight_id)
    if insight is None or insight.user_id != user.id:
        raise HTTPException(status_code=404, detail="Insight not found")
    skip = (insight.body or {}).get("safe_to_skip") or {}
    ids = [u for u in (parse_uuid(x) for x in skip.get("item_ids") or []) if u]
    owned = await user_owns_items(session, user.id, ids)
    await set_read(session, user.id, owned, True)
    body = dict(insight.body or {})
    body["safe_to_skip"] = dict(skip, done=True, marked=len(owned))
    insight.body = body
    await session.commit()
    ctx = {"insight": insight, "safe_to_skip": body["safe_to_skip"], "marked": len(owned)}
    return render(request, "partials/safe_to_skip.html", ctx, user=user, headers=hx_trigger("counts-changed"))
