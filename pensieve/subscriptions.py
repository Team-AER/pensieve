"""Subscription lifecycle. Callers own the transaction; no action deletes reader work."""

from datetime import UTC, datetime

from sqlalchemy import select

from pensieve.models import FEED_KIND_SAVED, Feed, SubscriptionEvent, User


def followed(feed=Feed):
    """Shared SQL predicate, deliberately independent of pause and delivery."""
    return feed.unfollowed_at.is_(None)


def snapshot(feed):
    return {
        "unfollowed_at": feed.unfollowed_at.isoformat() if feed.unfollowed_at else None,
        "paused": bool(feed.paused),
        "pause_reason": feed.pause_reason,
        "essential": bool(feed.essential),
        "delivery": feed.delivery,
    }


async def record(session, feed, action, previous_state, source):
    session.add(
        SubscriptionEvent(
            user_id=feed.user_id,
            feed_id=feed.id,
            action=action,
            previous_state=previous_state,
            source=source,
            created_at=datetime.now(UTC),
        )
    )
    # Counts may have been computed before the change in this process.
    from pensieve.web.queries import invalidate_nav_cache

    invalidate_nav_cache(feed.user_id)


async def _lock_user(session, feed):
    with session.no_autoflush:
        await session.execute(select(User.id).where(User.id == feed.user_id).with_for_update())


async def _lock(session, feed):
    await _lock_user(session, feed)
    await session.flush()
    return await session.scalar(
        select(Feed)
        .where(Feed.id == feed.id, Feed.user_id == feed.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def follow(session, feed, *, source="direct"):
    """Record a new subscription, or restore an existing one."""
    if feed.unfollowed_at is not None:
        return await restore(session, feed, source=source)
    await _lock_user(session, feed)
    await session.flush()
    await record(session, feed, "follow", {}, source)
    return feed


async def pause(session, feed, *, reason="user", source="web"):
    if reason not in {"user", "errors", "gone"}:
        raise ValueError("Unknown pause reason")
    feed = await _lock(session, feed)
    if feed.kind == FEED_KIND_SAVED or feed.unfollowed_at is not None:
        return feed
    previous = snapshot(feed)
    if feed.paused and feed.pause_reason == reason:
        return feed
    feed.paused, feed.pause_reason = True, reason
    await record(session, feed, "pause", previous, source)
    return feed


async def resume(session, feed, *, source="web"):
    feed = await _lock(session, feed)
    if feed.kind == FEED_KIND_SAVED or feed.unfollowed_at is not None:
        return feed
    previous = snapshot(feed)
    if not feed.paused:
        return feed
    feed.paused, feed.pause_reason = False, None
    feed.error_count, feed.last_error = 0, None
    feed.next_fetch_at = datetime.now(UTC)
    await record(session, feed, "resume", previous, source)
    return feed


async def unfollow(session, feed, *, source="web"):
    feed = await _lock(session, feed)
    if feed.kind == FEED_KIND_SAVED or feed.unfollowed_at is not None:
        return feed
    previous = snapshot(feed)
    feed.unfollowed_at, feed.paused = datetime.now(UTC), True
    await record(session, feed, "unfollow", previous, source)
    return feed


async def restore(session, feed, *, source="direct"):
    feed = await _lock(session, feed)
    if feed.unfollowed_at is None:
        return feed
    previous = snapshot(feed)
    feed.unfollowed_at, feed.paused, feed.pause_reason = None, False, None
    feed.error_count, feed.last_error = 0, None
    feed.next_fetch_at = datetime.now(UTC)
    await record(session, feed, "restore", previous, source)
    return feed
