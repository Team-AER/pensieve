"""Explicit stream choices, using the existing OPML insertion path."""

from xml.etree import ElementTree as ET

from sqlalchemy import select

from pensieve import subscriptions
from pensieve.fetch.opml import import_opml
from pensieve.models import Feed, StreamFollow, SubscriptionEvent, User
from pensieve.subscriptions import followed


async def lock_user(session, user):
    # All subscription creators take this lock, making retries/concurrent overlapping follows idempotent.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())


async def membership(session, user, slug):
    return await session.scalar(
        select(StreamFollow).where(StreamFollow.user_id == user.id, StreamFollow.slug == slug)
    )


async def save_choices(session, user, stream, version, choices, retired=(), catalog_urls=()):
    await lock_user(session, user)
    current = await membership(session, user, stream.slug)
    previous = set(current.chosen_urls if current else [])
    deselected = (previous & set(catalog_urls)) - set(choices)
    exclusive_urls = (
        {feed.url for feed, exclusive in await removal_choices(session, user, current) if exclusive}
        if current
        else set()
    )
    removals = (set(retired) & previous) | (deselected & exclusive_urls)
    # Retire only rows deliberately selected, never every catalog change.
    for feed in await session.scalars(
        select(Feed).where(Feed.user_id == user.id, Feed.url.in_(removals), followed())
    ):
        await subscriptions.unfollow(session, feed, source="stream:" + stream.slug)
    root = ET.Element("opml", version="2.0")
    body = ET.SubElement(root, "body")
    folder = ET.SubElement(body, "outline", text=stream.title)
    for url in dict.fromkeys(choices):
        ET.SubElement(folder, "outline", type="rss", xmlUrl=url, text=url)
    await import_opml(session, user, ET.tostring(root), source="stream:" + stream.slug, commit=False)
    if current is None:
        current = StreamFollow(user_id=user.id, slug=stream.slug, catalog_version=version, chosen_urls=[])
        session.add(current)
    current.catalog_version = version
    current.chosen_urls = sorted((previous - set(retired) - deselected) | set(choices))
    await session.flush()
    return current


async def removal_choices(session, user, current):
    """Default to removing only subscriptions actually acquired through this stream, with no other owner."""
    others = list(
        await session.scalars(
            select(StreamFollow).where(StreamFollow.user_id == user.id, StreamFollow.slug != current.slug)
        )
    )
    shared = {url for row in others for url in row.chosen_urls}
    feeds = list(
        await session.scalars(
            select(Feed).where(Feed.user_id == user.id, Feed.url.in_(current.chosen_urls), followed())
        )
    )
    rows = []
    for feed in feeds:
        events = list(
            await session.scalars(
                select(SubscriptionEvent)
                .where(
                    SubscriptionEvent.user_id == user.id,
                    SubscriptionEvent.feed_id == feed.id,
                    SubscriptionEvent.action.in_(["follow", "restore"]),
                )
                .order_by(SubscriptionEvent.created_at)
            )
        )
        latest = events[-1] if events else None
        exclusive = feed.url not in shared and latest is not None and latest.source.startswith("stream:")
        rows.append((feed, exclusive))
    return rows


async def unfollow_stream(session, user, current, selected):
    await lock_user(session, user)
    # The form permits overrides, but only for feeds belonging to this stream.
    for feed in await session.scalars(
        select(Feed).where(
            Feed.user_id == user.id, Feed.url.in_(set(selected) & set(current.chosen_urls)), followed()
        )
    ):
        await subscriptions.unfollow(session, feed, source="stream:" + current.slug)
    await session.delete(current)
