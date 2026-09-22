# ruff: noqa: F811  (fixtures imported from test_fetch_helpers are used as test parameters)
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pensieve import models, queue
from pensieve.config import get_settings
from pensieve.fetch import jobs, scheduler
from tests.test_fetch_helpers import fake_queue  # noqa: F401


async def test_due_feeds_enqueue_with_deterministic_job_ids(session, user, fake_queue):
    now = datetime.now(UTC)
    due = models.Feed(
        user_id=user.id, url="https://a.example.com/rss", next_fetch_at=now - timedelta(minutes=1)
    )
    later = models.Feed(
        user_id=user.id, url="https://b.example.com/rss", next_fetch_at=now + timedelta(hours=1)
    )
    paused = models.Feed(
        user_id=user.id, url="https://c.example.com/rss", next_fetch_at=now - timedelta(days=1), paused=True
    )
    never = models.Feed(user_id=user.id, url="https://d.example.com/rss", next_fetch_at=None)
    session.add_all([due, later, paused, never])
    await session.commit()

    assert await scheduler.enqueue_due_feeds(session) == 1
    assert fake_queue == [(queue.FETCH_FEED, (str(due.id),), queue.job_id_for("fetch", due.id))]


async def test_prune_old_items_respects_exemptions(session, user):
    retention = get_settings().item_retention_days
    old = datetime.now(UTC) - timedelta(days=retention + 5)
    feed = models.Feed(user_id=user.id, url="https://a.example.com/rss")
    session.add(feed)
    await session.flush()

    def item(guid, when):
        return models.Item(feed_id=feed.id, guid=guid, title=guid, published_at=when, hash="h" * 64)

    plain, starred, noted, canonical, cited, fresh = (
        item("plain", old),
        item("starred", old),
        item("noted", old),
        item("canonical", old),
        item("cited", old),
        item("fresh", datetime.now(UTC)),
    )
    session.add_all([plain, starred, noted, canonical, cited, fresh])
    await session.flush()
    session.add_all(
        [
            models.ItemState(user_id=user.id, item_id=starred.id, is_starred=True),
            models.ItemState(user_id=user.id, item_id=plain.id, is_read=True),
            models.Note(user_id=user.id, item_id=noted.id, body="n"),
            models.Cluster(user_id=user.id, window_start=old, window_end=old, canonical_item_id=canonical.id),
            models.Insight(user_id=user.id, kind="daily", period="2024-01-01", item_refs=[cited.id]),
        ]
    )
    await session.commit()

    assert await scheduler.prune_old_items(session) == 1
    await session.commit()
    remaining = set((await session.scalars(select(models.Item.guid))).all())
    assert remaining == {"starred", "noted", "canonical", "cited", "fresh"}


def test_jobs_registry():
    assert [f.__name__ for f in jobs.FUNCTIONS] == [
        "fetch_feed",
        "fetch_reader_mode",
        "fetch_due_feeds",
        "prune_old_items",
        "refresh_favicons",
    ]
    by_name = {c.name: c for c in jobs.CRON_JOBS}
    assert set(by_name) == {"cron:fetch_due_feeds", "cron:prune_old_items", "cron:refresh_favicons"}
    assert by_name["cron:refresh_favicons"].weekday == 0
    assert by_name["cron:fetch_due_feeds"].minute == set(range(60))
    assert by_name["cron:prune_old_items"].hour == 3 and by_name["cron:prune_old_items"].minute == 15
