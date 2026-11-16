import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from pensieve import models
from pensieve.ai.client import LLMError
from pensieve.fetch.discover import Discovered
from pensieve.fetch.parse import parse_feed
from pensieve.review.angles import run_review
from pensieve.review.evidence import preview_evidence
from pensieve.streams import load_catalog
from pensieve.streams.further import cited_domains
from pensieve.streams.preview import preview
from tests.test_feed_review import syndicated
from tests.test_web_support import login, memory_limiter, seed_feed, seed_item  # noqa: F401


async def test_preview_overlap_separates_measured_estimated_and_unknown(session, user):
    feed = await seed_feed(session, user)
    item = await seed_item(session, feed, "New telescope discovers distant planetary system")
    sample = {
        "entries": [
            {"url": item.url, "hash": "other", "title": "different title"},
            {"url": "https://other.example/story", "hash": "different", "title": item.title},
            {"url": None, "hash": "", "title": "A separate story"},
        ]
    }
    result = await preview_evidence(session, user, sample)
    assert result["sample_size"] == 3 and result["reader_sample_size"] == 1
    assert result["exact"] == 1 and result["estimated"] == 2 and result["potentially_added"] == 1
    item.published_at = datetime.now(UTC) - timedelta(days=15)
    await session.commit()
    result = await preview_evidence(session, user, sample)
    assert result["reader_sample_size"] == 0 and result["estimated"] == 0


async def test_cited_domains_are_local_and_probed_only_on_click(client, session, user, monkeypatch):
    feed = await seed_feed(session, user)
    now = datetime.now(UTC)
    for n in range(3):
        item = await seed_item(session, feed, f"Article {n}")
        item.content_html = '<p><a href="https://cited.example/report">Source</a><a href="https://cited.example/other">More</a></p>'
        session.add(
            models.ItemState(
                user_id=user.id, item_id=item.id, is_read=True, read_at=now - timedelta(minutes=n)
            )
        )
    await session.commit()
    assert await cited_domains(session, user) == [
        {"domain": "cited.example", "count": 3, "url": "https://cited.example"}
    ]
    mock = AsyncMock()
    monkeypatch.setattr("pensieve.web.discover.preview", mock)
    await login(client, user)
    response = await client.get("/discover")
    assert "Preview cited.example" in response.text
    mock.assert_not_called()


async def test_explore_and_dismissals_do_not_change_reading_or_subscriptions(client, session, user):
    source = load_catalog().streams[0].sources[0].url
    feed = models.Feed(user_id=user.id, url=source, title="NASA")
    session.add(feed)
    await session.commit()
    article = await seed_item(session, feed, "Space science report")
    headers = await login(client, user)
    response = await client.get(f"/discover/explore?item={article.id}")
    assert response.status_code == 200 and "Shares the science topic" in response.text
    assert source not in response.text
    other = load_catalog().streams[0].sources[1].url
    assert other in response.text
    assert (await client.post("/discover/dismiss", headers=headers, data={"url": other})).status_code == 303
    assert other not in (await client.get(f"/discover/explore?item={article.id}")).text
    assert other in (await client.get("/discover/dismissed")).text
    assert (
        await client.post("/discover/dismissed/reset", headers=headers, data={"url": other})
    ).status_code == 303
    assert other in (await client.get(f"/discover/explore?item={article.id}")).text
    assert await session.scalar(select(func.count(models.Feed.id))) == 1
    assert await session.scalar(select(func.count(models.ItemState.item_id))) == 0
    assert await session.scalar(select(func.count(models.SubscriptionEvent.id))) == 0


async def test_stale_preview_is_refetched_and_missing_dates_are_unknown(monkeypatch):
    class Cache:
        async def get(self, key):
            return json.dumps(
                {"measured_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(), "title": "Stale"}
            )

        async def set(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr("pensieve.streams.preview.Redis.from_url", lambda *args, **kwargs: Cache())
    parsed = parse_feed(
        b'<rss version="2.0"><channel><title>Fresh</title><item><guid>1</guid><title>One</title></item></channel></rss>',
        "https://example.org/rss",
    )
    fetch = AsyncMock(return_value=Discovered("https://canonical.example/rss", parsed))
    monkeypatch.setattr("pensieve.streams.preview.discover", fetch)
    result = await preview("https://alias.example/rss")
    assert result["title"] == "Fresh" and result["feed_url"] == "https://canonical.example/rss"
    assert result["posts_per_week"] is None and result["sample_size"] == 1
    fetch.assert_awaited_once()


async def test_ai_failure_keeps_feed_review_available(session, user):
    await syndicated(session, user)
    gateway = AsyncMock()
    gateway.chat_json.side_effect = LLMError("offline")
    row = await run_review(session, user, gateway)
    assert row.kind == "feed_review"
    assert all(result["status"] == "not checked" for result in row.body["feeds"].values())


async def test_new_follow_checkin_has_no_automatic_removal(client, session, user):
    await seed_feed(session, user, "New source")
    await login(client, user)
    response = await client.get("/manage/feed-review")
    assert "New-follow check-in" in response.text and "No reduction suggested" in response.text
    assert await session.scalar(select(func.count(models.SubscriptionEvent.id))) == 0
