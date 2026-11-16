"""One deliberate action at a time, always against current subscription evidence."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from pensieve.models import Feed, Item, ItemState, SubscriptionEvent, User
from pensieve.review import controls
from pensieve.review.angles import checked_angles
from pensieve.review.evidence import evidence
from pensieve.review.recommendations import recommend
from pensieve.web.manage import page
from pensieve.web.templating import DB, CsrfUser, CurrentUser

router = APIRouter(prefix="/manage/feed-review")


async def review_token(session, user):
    feeds = (
        await session.execute(
            select(Feed.id, Feed.unfollowed_at, Feed.paused, Feed.essential, Feed.delivery)
            .where(Feed.user_id == user.id)
            .order_by(Feed.id)
        )
    ).all()
    events = list(
        await session.scalars(
            select(SubscriptionEvent.id)
            .where(SubscriptionEvent.user_id == user.id)
            .order_by(SubscriptionEvent.id)
        )
    )
    items = (
        await session.execute(
            select(func.count(Item.id), func.max(Item.fetched_at))
            .join(Feed, Feed.id == Item.feed_id)
            .where(Feed.user_id == user.id)
        )
    ).one()
    states = await session.scalar(select(func.max(ItemState.updated_at)).where(ItemState.user_id == user.id))
    return hashlib.sha256(repr((feeds, events, items, states)).encode()).hexdigest()


@router.get("")
async def review_page(request: Request, user: CurrentUser, session: DB):
    now = datetime.now(UTC)
    rows = await evidence(session, user, now=now)
    angles = await checked_angles(session, user, rows)
    panels = [
        {
            "evidence": ev,
            "action": recommend(ev, angles.get(ev.feed.id)),
            "angle": angles.get(ev.feed.id, {"status": "not checked", "labels": []}),
            "new": ev.followed_since > now - timedelta(days=21),
        }
        for ev in rows
    ]
    panels.sort(
        key=lambda p: (not bool(p["action"]), not p["new"], -p["evidence"].per_day, p["evidence"].feed.title)
    )
    events = list(
        await session.scalars(
            select(SubscriptionEvent)
            .where(SubscriptionEvent.user_id == user.id, SubscriptionEvent.source == "feed_review")
            .order_by(SubscriptionEvent.created_at.desc())
            .limit(20)
        )
    )
    cfg = controls.config(user, now)
    if "feed_review" not in (user.settings or {}):
        await session.refresh(user, with_for_update=True)
        cfg = controls.config(user, now)
        controls.store_config(user, cfg)
    return page(
        request,
        user,
        "feed-review",
        "manage/feed_review.html",
        {
            "panels": panels,
            "cfg": cfg,
            "events": events,
            "review_token": await review_token(session, user),
            "total_per_day": round(sum(ev.per_day for ev in rows), 1),
            "notice": request.query_params.get("notice", ""),
        },
    )


@router.post("/action")
async def review_action(request: Request, user: CsrfUser, session: DB):
    form = await request.form()
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    if form.get("review_token") != await review_token(session, user):
        raise HTTPException(409, "Subscriptions changed. Reload the review before choosing another action.")
    try:
        feed = await session.get(Feed, uuid.UUID(str(form.get("feed_id"))))
        if feed is None or feed.user_id != user.id:
            raise ValueError("Unknown feed")
        # An old suggestion cannot authorize a reduction after the supporting evidence changes.
        fresh = await evidence(session, user)
        if form.get("suggested_action") and form.get("action") in {"quiet", "pause", "unfollow", "limit"}:
            angles = await checked_angles(session, user, fresh)
            ev = next((ev for ev in fresh if ev.feed.id == feed.id), None)
            if ev is None or recommend(ev, angles.get(feed.id)) is None:
                raise HTTPException(
                    409, "The supporting evidence changed. Reload the review before reducing this source."
                )
        await controls.apply(session, user, feed, str(form.get("action")), limit=int(form.get("limit") or 3))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return RedirectResponse(
        "/manage/feed-review?notice=Change+applied.+Remaining+evidence+has+been+recomputed.", status_code=303
    )


@router.post("/undo/{event_id}")
async def undo_action(event_id: uuid.UUID, user: CsrfUser, session: DB):
    try:
        await controls.undo(session, user, event_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return RedirectResponse("/manage/feed-review?notice=Change+undone.", status_code=303)


@router.post("/settings")
async def review_settings(request: Request, user: CsrfUser, session: DB):
    form = await request.form()
    await session.refresh(user, with_for_update=True)
    cfg = controls.config(user)
    try:
        cfg["target_per_day"] = max(1, min(10000, int(form.get("target_per_day") or 30)))
        snooze = int(form.get("snooze_days") or 0)
    except ValueError as exc:
        raise HTTPException(400, "Use a number of stories and snooze days") from exc
    cfg["enabled"] = form.get("enabled") == "1"
    cfg["snooze_until"] = (
        (datetime.now(UTC) + timedelta(days=max(0, min(90, snooze)))).isoformat() if snooze else None
    )
    controls.store_config(user, cfg)
    await session.commit()
    return RedirectResponse("/manage/feed-review?notice=Review+preferences+saved.", status_code=303)
