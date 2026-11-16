"""Explicit, undoable controls and bounded prompting. No subscription action runs in a job."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pensieve import subscriptions
from pensieve.models import Feed, SubscriptionEvent, User
from pensieve.review.evidence import evidence
from pensieve.review.recommendations import recommend

ACTIONS = {"keep", "unkeep", "limit", "quiet", "normal", "pause", "unfollow"}


def config(user, now=None):
    now = now or datetime.now(UTC)
    raw = (user.settings or {}).get("feed_review", {})
    return {
        "enabled": raw.get("enabled", True),
        "target_per_day": raw.get("target_per_day", 30),
        "started_at": raw.get("started_at", now.isoformat()),
        "snooze_until": raw.get("snooze_until"),
        "last_prompt_at": raw.get("last_prompt_at"),
    }


def store_config(user, cfg):
    user.settings = {**(user.settings or {}), "feed_review": cfg}


def date_value(value):
    try:
        return datetime.fromisoformat(value).astimezone(UTC) if value else None
    except (ValueError, TypeError):
        return None


async def prompt_due(session, user, now=None):
    now = now or datetime.now(UTC)
    await session.scalar(
        select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
    )
    cfg = config(user, now)
    if "feed_review" not in (user.settings or {}):
        store_config(user, cfg)
    if not cfg["enabled"] or now - (date_value(cfg["started_at"]) or now) < timedelta(days=14):
        return False
    if (date_value(cfg["snooze_until"]) or now) > now:
        return False
    if (date_value(cfg["last_prompt_at"]) or now - timedelta(days=8)) > now - timedelta(days=7):
        return False
    rows = await evidence(session, user, now=now)
    if sum(ev.per_day for ev in rows) <= cfg["target_per_day"] or not any(recommend(ev) for ev in rows):
        return False
    cfg["last_prompt_at"] = now.isoformat()
    store_config(user, cfg)
    return True


async def apply(session, user, feed, action, *, limit=3):
    if action not in ACTIONS or feed.user_id != user.id or feed.kind != "rss":
        raise ValueError("Unknown feed or action")
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    await session.refresh(user)
    await session.refresh(feed, with_for_update=True)
    if feed.unfollowed_at is not None:
        raise ValueError("This feed is already unfollowed")
    if action == "pause":
        return await subscriptions.pause(session, feed, source="feed_review")
    if action == "unfollow":
        return await subscriptions.unfollow(session, feed, source="feed_review")
    previous = subscriptions.snapshot(feed)
    if action in {"keep", "unkeep"}:
        feed.essential = action == "keep"
    elif action in {"quiet", "normal"}:
        feed.delivery = "quiet" if action == "quiet" else "normal"
    elif action == "limit":
        settings = dict(user.settings or {})
        paper = dict(settings.get("paper") or {})
        limits = dict(paper.get("feed_limits") or {})
        previous["paper_limit"] = limits.get(str(feed.id))
        limits[str(feed.id)] = max(1, min(100, int(limit)))
        paper["feed_limits"] = limits
        user.settings = {**settings, "paper": paper}
    await subscriptions.record(session, feed, action, previous, "feed_review")
    return feed


async def undo(session, user, event_id):
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    event = await session.get(SubscriptionEvent, event_id)
    if event is None or event.user_id != user.id or event.feed_id is None or event.action == "undo":
        raise ValueError("This change cannot be undone")
    feed = await session.get(Feed, event.feed_id, with_for_update=True)
    latest = await session.scalar(
        select(SubscriptionEvent)
        .where(SubscriptionEvent.feed_id == feed.id)
        .order_by(SubscriptionEvent.created_at.desc(), SubscriptionEvent.id.desc())
        .limit(1)
    )
    if latest.id != event.id or not event.previous_state:
        raise ValueError("A newer change exists; review this feed's current settings")
    previous = subscriptions.snapshot(feed)
    state = event.previous_state
    for key in ("paused", "pause_reason", "essential", "delivery"):
        if key in state:
            setattr(feed, key, state[key])
    feed.unfollowed_at = date_value(state.get("unfollowed_at"))
    if not feed.paused and feed.unfollowed_at is None:
        feed.next_fetch_at = datetime.now(UTC)
    if "paper_limit" in state:
        settings = dict(user.settings or {})
        paper = dict(settings.get("paper") or {})
        limits = dict(paper.get("feed_limits") or {})
        previous["paper_limit"] = limits.get(str(feed.id))
        if state["paper_limit"] is None:
            limits.pop(str(feed.id), None)
        else:
            limits[str(feed.id)] = state["paper_limit"]
        paper["feed_limits"] = limits
        user.settings = {**settings, "paper": paper}
    await subscriptions.record(session, feed, "undo", previous, "undo:" + str(event.id))
    return feed
