from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pensieve import models, subscriptions
from pensieve.config import get_settings
from pensieve.fetch.parse import parse_feed
from pensieve.streams import load_catalog
from pensieve.streams.preview import volume
from pensieve.streams.service import membership, removal_choices, save_choices, unfollow_stream
from tests.test_web_support import login, memory_limiter, seed_feed  # noqa: F401


@pytest.fixture
def previews(monkeypatch):
    async def fake(url):
        return {
            "url": url,
            "feed_url": url,
            "title": "Preview source",
            "measured_at": datetime.now(UTC).isoformat(),
            "posts_per_week": None,
            "sample_size": 0,
            "entries": [],
            "error": None,
        }

    mock = AsyncMock(side_effect=fake)
    monkeypatch.setattr("pensieve.web.discover.preview", mock)
    return mock


async def test_preview_and_search_never_subscribe(client, session, user, previews):
    user.ai_enabled = False
    await session.commit()
    await login(client, user)
    r = await client.get("/discover?topic=science&language=en")
    assert r.status_code == 200 and "Space and science" in r.text and "Open software" not in r.text
    r = await client.get("/discover/streams/space-science")
    assert r.status_code == 200 and "Volume unknown" in r.text
    assert await session.scalar(select(func.count(models.Feed.id))) == 0
    assert await session.scalar(select(func.count(models.Item.id))) == 0
    assert await session.scalar(select(func.count(models.StreamFollow.id))) == 0


async def test_overlapping_streams_retries_and_direct_ownership(session, user):
    catalog = load_catalog()
    first, second = catalog.streams[1:]
    shared = first.sources[-1].url
    direct = await seed_feed(session, user)
    await subscriptions.follow(session, direct)
    await save_choices(session, user, first, catalog.version, [shared, direct.url])
    await session.commit()
    await save_choices(session, user, first, catalog.version, [shared, direct.url])
    await save_choices(session, user, second, catalog.version, [shared])
    await session.commit()
    assert await session.scalar(select(func.count(models.Feed.id))) == 2
    assert await session.scalar(select(func.count(models.StreamFollow.id))) == 2
    current = await membership(session, user, first.slug)
    assert all(not exclusive for _, exclusive in await removal_choices(session, user, current))
    await unfollow_stream(session, user, current, [])
    await session.commit()
    assert (
        await session.scalar(select(func.count(models.Feed.id)).where(models.Feed.unfollowed_at.is_(None)))
        == 2
    )
    current = await membership(session, user, second.slug)
    # After the first stream is removed, the remaining stream exclusively owns the shared subscription.
    assert (await removal_choices(session, user, current))[0][1]
    await unfollow_stream(session, user, current, [shared])
    await session.commit()
    assert (await session.scalar(select(models.Feed).where(models.Feed.url == shared))).unfollowed_at
    await session.refresh(direct)
    assert direct.unfollowed_at is None


async def test_follow_partial_preview_failure_and_alias(client, session, user, previews):
    headers = await login(client, user)
    stream = load_catalog().streams[0]

    async def fake(url):
        return {
            "url": url,
            "feed_url": "https://canonical.example/rss" if url == stream.sources[0].url else url,
            "title": "Source",
            "measured_at": datetime.now(UTC).isoformat(),
            "posts_per_week": None,
            "sample_size": 0,
            "entries": [],
            "error": "Unavailable" if url == stream.sources[1].url else None,
        }

    previews.side_effect = fake
    for _ in range(2):
        r = await client.post(
            f"/discover/streams/{stream.slug}/follow",
            data={"urls": [s.url for s in stream.sources]},
            headers=headers,
        )
        assert r.status_code == 303, r.text
    feeds = list(await session.scalars(select(models.Feed)))
    assert {f.url for f in feeds} == {"https://canonical.example/rss", stream.sources[1].url}
    folder = await session.scalar(select(models.Folder).where(models.Folder.name == stream.title))
    assert all(f.folder_id == folder.id for f in feeds)
    assert all(f.next_fetch_at and not f.paused for f in feeds)
    assert await session.scalar(select(func.count(models.Item.id))) == 0
    bad = await client.post(
        f"/discover/streams/{stream.slug}/follow",
        data={"urls": "https://injected.example/rss"},
        headers=headers,
    )
    assert bad.status_code == 400


async def test_catalog_update_and_unfollow_are_explicit(client, session, user, previews):
    headers = await login(client, user)
    stream = load_catalog().streams[0]
    await save_choices(session, user, stream, 0, ["https://retired.example/rss", stream.sources[0].url])
    await session.commit()
    r = await client.get(f"/discover/streams/{stream.slug}")
    assert "Retired sources" in r.text and "Catalog version" in r.text
    assert 'name="retired" value="https://retired.example/rss"' in r.text
    assert (
        await session.scalar(select(func.count(models.Feed.id)).where(models.Feed.unfollowed_at.is_(None)))
        == 2
    )
    r = await client.post(
        f"/discover/streams/{stream.slug}/follow",
        headers=headers,
        data={"urls": stream.sources[0].url, "retired": "https://retired.example/rss"},
    )
    assert r.status_code == 303
    assert (
        await session.scalar(select(models.Feed).where(models.Feed.url == "https://retired.example/rss"))
    ).unfollowed_at
    current = await membership(session, user, stream.slug)
    await session.refresh(current)
    assert current.catalog_version == 1 and current.chosen_urls == [stream.sources[0].url]
    rows = await removal_choices(session, user, current)
    assert len(rows) == 1 and rows[0][1]


def test_volume_never_uses_synthetic_entry_dates():
    parsed = parse_feed(
        b'<rss version="2.0"><channel><title>No dates</title><item><title>A</title><guid>a</guid></item><item><title>B</title><guid>b</guid></item></channel></rss>',
        "https://example.org/rss",
    )
    assert all(not e.date_known for e in parsed.entries)
    assert volume(parsed.entries, datetime.now(UTC)) is None
    parsed.entries[0].published_at = datetime.now(UTC) - timedelta(days=7)
    parsed.entries[1].published_at = datetime.now(UTC)
    for entry in parsed.entries:
        entry.date_known = True
    assert volume(parsed.entries, datetime.now(UTC)) == 1.0


def test_catalog_override_is_validated(tmp_path, monkeypatch):
    path = tmp_path / "catalog.toml"
    path.write_text("version = 2\nstreams = []\n")
    monkeypatch.setattr(get_settings(), "stream_catalog_path", str(path))
    with pytest.raises(ValueError):
        load_catalog()


async def test_concurrent_overlapping_follows_are_one_subscription(session, user, engine):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    catalog = load_catalog()
    first, second = catalog.streams[1:]
    shared = first.sources[-1].url
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def follow_one(stream):
        async with maker() as other:
            owner = await other.get(models.User, user.id)
            await save_choices(other, owner, stream, catalog.version, [shared])
            await other.commit()

    await asyncio.gather(follow_one(first), follow_one(second))
    assert await session.scalar(select(func.count(models.Feed.id))) == 1
    assert await session.scalar(select(func.count(models.StreamFollow.id))) == 2
