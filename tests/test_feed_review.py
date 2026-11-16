import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pensieve import models, subscriptions
from pensieve.review import controls
from pensieve.review.angles import checked_angles, run_review
from pensieve.review.evidence import evidence
from pensieve.review.recommendations import recommend, shortlisted
from pensieve.web.reader import View, nav_data, view_counts
from tests.test_web_support import login, memory_limiter  # noqa: F401


async def syndicated(session, user, *, count=28, text="Full reporting and details. " * 20, acquired=True):
    now = datetime.now(UTC)
    feeds = [
        models.Feed(
            user_id=user.id,
            url=f"https://{n}.example.org/rss",
            title=f"Source {n}",
            created_at=now - timedelta(days=60),
            last_success_at=now,
        )
        for n in range(2)
    ]
    session.add_all(feeds)
    await session.flush()
    for day in range(count):
        for feed in feeds:
            when = now - timedelta(days=day, hours=1)
            session.add(
                models.Item(
                    feed_id=feed.id,
                    guid=str(day),
                    title=f"Story {day}",
                    url=f"https://news.example.org/{day}",
                    hash=str(day).zfill(64),
                    published_at=when,
                    fetched_at=when if acquired else now,
                    content_text=text,
                    content_html=f"<p>{text}</p>",
                )
            )
    await session.commit()
    return feeds


async def test_syndicated_evidence_ai_off_and_durable_undo(session, user, engine):
    feeds = await syndicated(session, user)
    rows = await evidence(session, user)
    assert len(rows) == 2
    assert all(
        ev.size == 28 and ev.exact == 28 and ev.shared == 28 and ev.healthy and not ev.only_here
        for ev in rows
    )
    assert all(recommend(ev) == "quiet" for ev in rows)
    user.ai_enabled = False
    client = AsyncMock()
    insight = await run_review(session, user, client)
    assert insight.kind == "feed_review" and insight.item_refs
    client.chat_json.assert_not_called()
    assert all(value["status"] == "not checked" for value in insight.body["feeds"].values())
    await controls.apply(session, user, feeds[0], "quiet")
    await session.commit()
    event = await session.scalar(
        select(models.SubscriptionEvent).where(models.SubscriptionEvent.action == "quiet")
    )
    assert (await view_counts(session, user, View("unread")))[0] == 28
    assert (await view_counts(session, user, View("feed", feed_id=feeds[0].id)))[0] == 28
    assert (await nav_data(session, user, View("unread"))).total_unread == 28
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as other:
        other_user = await other.get(models.User, user.id)
        restored = await controls.undo(other, other_user, event.id)
        await other.commit()
        assert restored.delivery == "normal"
    assert not list(await session.scalars(select(models.ItemState)))


@pytest.mark.parametrize("failure", ["sparse", "outage", "spike", "incomplete", "essential"])
async def test_uncertain_recommendations_withheld(session, user, failure):
    feeds = await syndicated(
        session,
        user,
        count=4 if failure == "sparse" else 28,
        text="teaser" if failure == "incomplete" else "Detailed reporting. " * 20,
        acquired=failure != "spike",
    )
    if failure == "outage":
        feeds[0].error_count = 2
    if failure == "essential":
        feeds[0].essential = True
    await session.commit()
    ev = next(ev for ev in await evidence(session, user) if ev.feed.id == feeds[0].id)
    assert recommend(ev) is None


async def test_rare_valuable_post_and_user_correction_protect_sources(session, user):
    feeds = await syndicated(session, user)
    now = datetime.now(UTC)
    rare = models.Item(
        feed_id=feeds[0].id,
        guid="rare",
        title="Local investigation",
        url="https://local.example/report",
        hash="rare",
        published_at=now,
        content_text="Detailed local reporting. " * 20,
    )
    session.add(rare)
    await session.flush()
    session.add(models.ItemState(user_id=user.id, item_id=rare.id, is_read=True, read_at=now))
    await session.commit()
    ev = next(ev for ev in await evidence(session, user) if ev.feed.id == feeds[0].id)
    assert ev.opened_only_here == [rare] and recommend(ev) is None
    pair = [
        await session.scalar(
            select(models.Item).where(models.Item.feed_id == feed.id, models.Item.guid == "0")
        )
        for feed in feeds
    ]
    session.add(models.ClusterOverride(user_id=user.id, item_a=pair[0].id, item_b=pair[1].id, action="split"))
    await session.commit()
    rows = await evidence(session, user)
    assert all(ev.exact == 27 for ev in rows)


@pytest.mark.parametrize(
    "label", ["added_reporting", "analysis", "local_detail", "disagreement", "uncertain"]
)
async def test_differing_angles_withheld(session, user, label):
    await syndicated(session, user)
    client = AsyncMock()
    client.chat_json.return_value = {"labels": [label] * 5}
    await run_review(session, user, client)
    await session.commit()
    rows = await evidence(session, user)
    angles = await checked_angles(session, user, rows)
    assert client.chat_json.call_count == 2
    assert all(recommend(ev, angles[ev.feed.id]) is None for ev in rows)


async def test_subscription_changes_cannot_apply_two_stale_removals(client, session, user):
    feeds = await syndicated(session, user)
    headers = await login(client, user)
    page = await client.get("/manage/feed-review")
    assert page.status_code == 200, page.text
    token = re.search(r'name="review_token" value="([^"]+)"', page.text)[1]
    for index, feed in enumerate(feeds):
        result = await client.post(
            "/manage/feed-review/action",
            headers=headers,
            data={
                "feed_id": str(feed.id),
                "action": "unfollow",
                "review_token": token,
            },
        )
        assert result.status_code == (303 if index == 0 else 409), result.text
    remaining = await evidence(session, user)
    assert len(remaining) == 1 and remaining[0].shared == 0 and recommend(remaining[0]) is None


async def test_prompt_budget_snooze_off_and_keep_ninety_days(session, user):
    feeds = await syndicated(session, user)
    now = datetime.now(UTC)
    assert not await controls.prompt_due(session, user, now)
    cfg = controls.config(user)
    cfg.update(started_at=(now - timedelta(days=15)).isoformat(), target_per_day=1)
    controls.store_config(user, cfg)
    assert await controls.prompt_due(session, user, now)
    assert not await controls.prompt_due(session, user, now)
    cfg = controls.config(user)
    cfg.update(last_prompt_at=None, snooze_until=(now + timedelta(days=30)).isoformat())
    controls.store_config(user, cfg)
    assert not await controls.prompt_due(session, user, now)
    cfg.update(snooze_until=None, enabled=False)
    controls.store_config(user, cfg)
    assert not await controls.prompt_due(session, user, now)
    await controls.apply(session, user, feeds[0], "keep")
    await session.commit()
    await controls.apply(session, user, feeds[0], "unkeep")
    await session.commit()
    ev = next(ev for ev in await evidence(session, user) if ev.feed.id == feeds[0].id)
    assert ev.recently_kept and not shortlisted(ev)


async def test_pausing_or_failed_peer_withholds_remaining_recommendation(session, user):
    feeds = await syndicated(session, user)
    assert all(recommend(ev) == "quiet" for ev in await evidence(session, user))
    await subscriptions.pause(session, feeds[0])
    await session.commit()
    other = next(ev for ev in await evidence(session, user) if ev.feed.id == feeds[1].id)
    assert other.shared == 28 and other.available_shared == 0 and recommend(other) is None


async def test_high_volume_without_repetition_is_not_a_reduction_signal(session, user):
    feeds = await syndicated(session, user)
    for it in await session.scalars(select(models.Item).where(models.Item.feed_id == feeds[0].id)):
        it.url += "/unique"
        it.hash = "f" + it.hash[1:]
    await session.commit()
    rows = await evidence(session, user)
    assert all(ev.shared == 0 and ev.healthy and recommend(ev) is None for ev in rows)


async def test_undo_keep_restores_suggestions_and_quiet_is_not_suggested_twice(session, user):
    feeds = await syndicated(session, user)
    await controls.apply(session, user, feeds[0], "keep")
    await session.commit()
    event = await session.scalar(
        select(models.SubscriptionEvent).where(models.SubscriptionEvent.action == "keep")
    )
    await controls.undo(session, user, event.id)
    await session.commit()
    ev = next(ev for ev in await evidence(session, user) if ev.feed.id == feeds[0].id)
    assert recommend(ev) == "quiet"
    await controls.apply(session, user, feeds[0], "quiet")
    await session.commit()
    ev = next(ev for ev in await evidence(session, user) if ev.feed.id == feeds[0].id)
    assert recommend(ev) is None
    from pensieve.syncapi.common import unread_by_feed

    assert len(await unread_by_feed(session, user.id)) == 2  # Third-party clients keep Quiet normal.


async def test_story_cluster_alone_does_not_mean_same_content(session, user):
    feeds = await syndicated(session, user)
    for day in range(28):
        pair = [
            await session.scalar(
                select(models.Item).where(models.Item.feed_id == feed.id, models.Item.guid == str(day))
            )
            for feed in feeds
        ]
        pair[0].hash = "f" + pair[0].hash[1:]
        pair[0].url += "/local"
        cluster = models.Cluster(
            user_id=user.id,
            kind="story",
            canonical_item_id=pair[0].id,
            window_start=pair[0].published_at,
            window_end=pair[1].published_at,
        )
        session.add(cluster)
        await session.flush()
        session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=it.id) for it in pair])
    await session.commit()
    rows = await evidence(session, user)
    assert all(ev.exact == 0 and ev.shared == 28 and recommend(ev) is None for ev in rows)
    gateway = AsyncMock()
    gateway.chat_json.return_value = {"labels": ["local_detail"] * 5}
    await run_review(session, user, gateway)
    await session.commit()
    angles = await checked_angles(session, user, rows)
    assert all(recommend(ev, angles[ev.feed.id]) is None for ev in rows)


async def test_concurrent_prompts_share_one_weekly_budget(session, user, engine):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    await syndicated(session, user)
    now = datetime.now(UTC)
    cfg = controls.config(user)
    cfg.update(started_at=(now - timedelta(days=15)).isoformat(), target_per_day=1)
    controls.store_config(user, cfg)
    await session.commit()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def prompt():
        async with maker() as other:
            owner = await other.get(models.User, user.id)
            due = await controls.prompt_due(other, owner, now)
            await other.commit()
            return due

    assert sorted(await asyncio.gather(prompt(), prompt())) == [False, True]


async def test_recompute_refreshes_health_from_other_sessions(session, user, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    feeds = await syndicated(session, user)
    assert all(recommend(ev) == "quiet" for ev in await evidence(session, user))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as other:
        peer = await other.get(models.Feed, feeds[0].id)
        peer.paused = True
        peer.pause_reason = "errors"
        await other.commit()
    rows = await evidence(session, user)
    assert all(recommend(ev) is None for ev in rows)
