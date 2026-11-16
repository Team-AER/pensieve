"""Lifecycle invariants across database sessions, workers, reader and sync consumers."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pensieve import models, subscriptions
from pensieve.ai.common import user_feed_ids
from pensieve.fetch import jobs, scheduler
from pensieve.fetch.opml import import_opml
from pensieve.fetch.subscribe import add_feed
from pensieve.syncapi.common import Stream, fetch_stream, unread_by_feed
from pensieve.web import reader
from tests.test_fetch_helpers import fake_queue  # noqa: F401
from tests.test_web_support import login, memory_limiter, seed_feed, seed_item  # noqa: F401


async def test_restore_survives_new_session_and_preserves_work(session, user, engine):
    feed = await seed_feed(session, user)
    item = await seed_item(session, feed, "Saved work")
    state = models.ItemState(user_id=user.id, item_id=item.id, is_read=True, is_starred=True)
    note = models.Note(user_id=user.id, item_id=item.id, body="keep this")
    item.archive_text = "Archived page"
    snapshot = models.Snapshot(user_id=user.id, item_id=item.id, requested_url=item.url, status="done")
    session.add_all([state, note, snapshot])
    await subscriptions.unfollow(session, feed)
    await session.commit()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as other:
        restored = await other.get(models.Feed, feed.id)
        assert restored.paused and restored.unfollowed_at
        await subscriptions.restore(other, restored)
        await other.commit()
        assert restored.id == feed.id and not restored.paused and restored.unfollowed_at is None
        assert (await other.get(models.Item, item.id)).archive_text == "Archived page"
        assert (await other.get(models.ItemState, (user.id, item.id))).is_read
        assert (await other.get(models.Note, note.id)).body == "keep this"
        assert (await other.get(models.Snapshot, snapshot.id)).status == "done"
        events = list(
            await other.scalars(
                select(models.SubscriptionEvent).order_by(models.SubscriptionEvent.created_at)
            )
        )
        assert [event.action for event in events] == ["unfollow", "restore"]
        assert events[-1].previous_state["unfollowed_at"]


async def test_unfollow_visibility_and_queued_fetch(session, user, monkeypatch):
    feed = await seed_feed(session, user)
    item = await seed_item(session, feed, "Retained")
    session.add(models.ItemState(user_id=user.id, item_id=item.id, is_starred=True))
    await subscriptions.unfollow(session, feed)
    await session.commit()
    fetch = AsyncMock()
    monkeypatch.setattr(jobs, "refresh_feed", fetch)
    assert await jobs.fetch_feed({}, str(feed.id)) == 0
    fetch.assert_not_called()
    assert await session.scalar(user_feed_ids(user.id)) is None
    assert await unread_by_feed(session, user.id) == []
    assert not (await fetch_stream(session, user.id, Stream("reading-list"), n=20)).rows
    assert (await fetch_stream(session, user.id, Stream("starred"), n=20)).rows[0][0].id == item.id
    assert (await reader.view_counts(session, user, reader.View("unread"))) == (0, 0)
    assert (await reader.view_counts(session, user, reader.View("starred"))) == (1, 1)


async def test_add_and_opml_restore_same_id(session, user, fake_queue):  # noqa: F811
    feed = await seed_feed(session, user)
    await subscriptions.unfollow(session, feed)
    await session.commit()
    assert (await add_feed(session, user, feed.url)).id == feed.id
    await subscriptions.unfollow(session, feed)
    await session.commit()
    xml = f'<opml><body><outline xmlUrl="{feed.url}"/><outline xmlUrl="{feed.url}"/></body></opml>'.encode()
    result = await import_opml(session, user, xml)
    assert [f.id for f in result.added] == [feed.id]
    assert result.skipped == [feed.url]
    assert feed.unfollowed_at is None and not feed.paused


async def test_resume_all_only_errors_and_restore_web(session, user, client):
    feeds = [
        await seed_feed(session, user, paused=True, pause_reason=reason)
        for reason in ("user", "errors", "gone")
    ]
    gone = await seed_feed(session, user)
    await subscriptions.unfollow(session, gone)
    saved = models.Feed(user_id=user.id, url=models.SAVED_FEED_URL, kind="saved", paused=True)
    session.add(saved)
    await session.commit()
    headers = await login(client, user)
    assert (await client.post("/manage/feeds/resume-all", headers=headers)).status_code == 303
    for feed in [*feeds, gone, saved]:
        await session.refresh(feed)
    assert [f.paused for f in feeds] == [True, False, True]
    assert gone.paused and saved.paused
    page = await client.get("/manage/feeds")
    assert "Unfollowed" in page.text and f"{gone.id}/restore" in page.text
    assert (await client.post(f"/manage/feeds/{gone.id}/restore", headers=headers)).status_code == 303
    await session.refresh(gone)
    assert not gone.paused and gone.unfollowed_at is None


async def test_unfollowed_retention_uses_unfollow_date_and_protects_work(session, user):
    now = datetime.now(UTC)
    expired = await seed_feed(session, user)
    recent = await seed_feed(session, user)
    empty = await seed_feed(session, user)
    for feed in (expired, recent, empty):
        await subscriptions.unfollow(session, feed)
    expired.unfollowed_at = empty.unfollowed_at = now - timedelta(days=31)
    plain = await seed_item(session, expired, "New but expired subscription")
    protected = await seed_item(session, expired, "Starred")
    retained = await seed_item(session, recent, "Recent unfollow")
    session.add(models.ItemState(user_id=user.id, item_id=protected.id, is_starred=True))
    await session.commit()
    assert await scheduler.prune_old_items(session, now) == 1
    await session.commit()
    remaining = set(await session.scalars(select(models.Item.id)))
    assert plain.id not in remaining and {protected.id, retained.id} <= remaining
    assert await session.scalar(select(models.Feed.id).where(models.Feed.id == empty.id)) is None
    event = await session.scalar(
        select(models.SubscriptionEvent).where(
            models.SubscriptionEvent.action == "unfollow", models.SubscriptionEvent.feed_id.is_(None)
        )
    )
    assert event is not None


async def test_sync_quickadd_restores_retained_feed_and_search_keeps_it(client, session, user):
    from tests.test_syncapi_helpers import auth, seed

    data = await seed(session, user)
    await subscriptions.unfollow(session, data.feed_a)
    await session.commit()
    headers = await login(client, user)
    search = await client.get("/search?q=Item")
    assert f"/items/{data.a1.id}" in search.text
    result = await client.post(
        "/reader/api/0/subscription/quickadd", headers=auth(data.token), data={"quickadd": data.feed_a.url}
    )
    assert result.status_code == 200 and result.json()["numResults"] == 1
    await session.refresh(data.feed_a)
    assert data.feed_a.unfollowed_at is None and not data.feed_a.paused
    assert headers
