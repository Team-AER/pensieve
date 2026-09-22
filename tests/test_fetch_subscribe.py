# ruff: noqa: F811  (fixtures imported from test_fetch_helpers are used as test parameters)
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from pensieve import models, queue
from pensieve.config import get_settings
from pensieve.fetch.subscribe import (
    FeedError,
    add_feed,
    backoff_interval,
    lengthen_interval,
    refresh_feed,
    shorten_interval,
)
from tests.test_fetch_helpers import (
    ATOM,
    HTML_NO_LINKS,
    HTML_WITH_LINKS,
    RSS,
    RSS_MORE,
    fake_queue,  # noqa: F401
    no_dns,  # noqa: F401
)

FEED_URL = "https://example.com/feed.xml"


def _router(**kwargs) -> respx.MockRouter:
    router = respx.mock(assert_all_called=False, **kwargs)
    return router


async def _make_feed(session, user, url=FEED_URL, **kw) -> models.Feed:
    feed = models.Feed(user_id=user.id, url=url, title="t", next_fetch_at=datetime.now(UTC), **kw)
    session.add(feed)
    await session.commit()
    return feed


async def _items(session, feed) -> list[models.Item]:
    return list((await session.scalars(select(models.Item).where(models.Item.feed_id == feed.id))).all())


# ---------------------------------------------------------------------------------------------- add_feed


async def test_add_feed_direct_feed_url(session, user, fake_queue):
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS, headers={"ETag": '"abc"', "Last-Modified": "x"})
        router.get("https://example.com/favicon.ico").respond(404)
        feed = await add_feed(session, user, FEED_URL)

    assert feed.id is not None and feed.user_id == user.id
    assert feed.url == FEED_URL and feed.title == "Example & Co" and feed.site_url == "https://example.com/"
    assert feed.icon_url == "https://example.com/logo.png"
    assert feed.websub_hub == "https://hub.example.com/"
    assert feed.etag == '"abc"' and feed.last_modified == "x"
    assert (
        feed.error_count == 0 and feed.last_success_at is not None and feed.next_fetch_at > feed.last_fetch_at
    )
    items = await _items(session, feed)
    assert {i.guid for i in items} == {"post-1", "https://example.com/posts/2"}

    names = [c[0] for c in fake_queue]
    assert names == [queue.AI_PROCESS_NEW_ITEMS, queue.AI_FILE_FEED]
    _, (feed_id, item_ids), _ = fake_queue[0]
    assert feed_id == str(feed.id) and set(item_ids) == {str(i.id) for i in items}
    assert fake_queue[1][1] == (str(feed.id),)


async def test_add_feed_with_folder_skips_filing(session, user, fake_queue):
    folder = models.Folder(user_id=user.id, name="Tech")
    session.add(folder)
    await session.commit()
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        router.get("https://example.com/favicon.ico").respond(404)
        feed = await add_feed(session, user, FEED_URL, folder_id=folder.id)
    assert feed.folder_id == folder.id
    assert [c[0] for c in fake_queue] == [queue.AI_PROCESS_NEW_ITEMS]


async def test_add_feed_autodiscovers_from_html(session, user, fake_queue):
    with _router() as router:
        router.get("https://example.com/").respond(
            200, content=HTML_WITH_LINKS, headers={"content-type": "text/html"}
        )
        router.get("https://example.com/feeds/all.rss").respond(200, content=ATOM)
        feed = await add_feed(session, user, "example.com")
    assert feed.url == "https://example.com/feeds/all.rss"
    assert feed.title == "Atom Example"
    assert feed.websub_hub == "https://pubsubhubbub.example.org/"  # feed's own hub wins over the page's
    assert feed.icon_url == "https://example.com/icon.png"  # relative icon resolved against the feed document
    assert len(await _items(session, feed)) == 2


async def test_add_feed_uses_page_hub_and_icon_when_feed_has_none(session, user, fake_queue):
    plain = b'<rss version="2.0"><channel><title>Plain</title><item><title>a</title><link>https://example.com/a</link></item></channel></rss>'
    with _router() as router:
        router.get("https://example.com/blog").respond(
            200, content=HTML_WITH_LINKS, headers={"content-type": "text/html"}
        )
        router.get("https://example.com/feeds/all.rss").respond(200, content=plain)
        feed = await add_feed(session, user, "https://example.com/blog")
    assert feed.websub_hub == "https://hub.example.com/"
    assert feed.icon_url == "https://example.com/static/fav.png"
    assert feed.site_url == "https://example.com/blog"


async def test_add_feed_well_known_fallback_and_favicon(session, user, fake_queue):
    with _router() as router:
        router.get("https://example.com/").respond(
            200, content=HTML_NO_LINKS, headers={"content-type": "text/html"}
        )
        router.get("https://example.com/feed").respond(404)
        router.get("https://example.com/rss").respond(
            200, content=RSS.replace(b"<image>", b"<x>").replace(b"</image>", b"</x>")
        )
        router.get("https://example.com/favicon.ico").respond(
            200, content=b"\x00\x00\x01\x00", headers={"content-type": "image/x-icon"}
        )
        feed = await add_feed(session, user, "https://example.com/")
    assert feed.url == "https://example.com/rss"
    assert feed.icon_url == "https://example.com/favicon.ico"


async def test_add_feed_duplicate_raises(session, user, fake_queue):
    await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        with pytest.raises(FeedError, match="already subscribed"):
            await add_feed(session, user, FEED_URL)
        # Discovered URL differs from the pasted one but is already stored
        router.get("https://example.com/").respond(
            200, content=HTML_NO_LINKS, headers={"content-type": "text/html"}
        )
        router.get("https://example.com/feed").respond(301, headers={"Location": FEED_URL})
        with pytest.raises(FeedError, match="already subscribed"):
            await add_feed(session, user, "https://example.com/")


async def test_add_feed_failures_raise_feed_error(session, user, fake_queue):
    with _router() as router:
        router.get("https://example.com/").respond(
            200, content=HTML_NO_LINKS, headers={"content-type": "text/html"}
        )
        router.get(url__regex=r"https://example\.com/.+").respond(404)
        with pytest.raises(FeedError, match="no feed found"):
            await add_feed(session, user, "https://example.com/")
        router.get("https://down.example.com/").respond(503)
        with pytest.raises(FeedError, match="503"):
            await add_feed(session, user, "https://down.example.com/")
    with pytest.raises(FeedError, match="cannot be fetched"):
        await add_feed(session, user, "http://127.0.0.1/feed")
    with pytest.raises(FeedError):
        await add_feed(session, user, "   ")
    assert fake_queue == []
    assert (await session.scalar(select(models.Feed).where(models.Feed.user_id == user.id))) is None


# ---------------------------------------------------------------------------------------------- refresh_feed


async def test_refresh_conditional_get_304(session, user, fake_queue):
    feed = await _make_feed(session, user, etag='"e1"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
    interval_before = feed.fetch_interval_min
    with _router() as router:
        route = router.get(FEED_URL).respond(304)
        assert await refresh_feed(session, feed) == []
    req = route.calls.last.request
    assert (
        req.headers["if-none-match"] == '"e1"'
        and req.headers["if-modified-since"] == "Mon, 01 Jan 2024 00:00:00 GMT"
    )
    assert feed.last_success_at is not None and feed.error_count == 0
    assert feed.fetch_interval_min > interval_before
    assert feed.etag == '"e1"'
    assert fake_queue == []


async def test_refresh_inserts_new_items_and_dedups_guid(session, user, fake_queue):
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS, headers={"ETag": '"v1"'})
        first = await refresh_feed(session, feed)
        assert {i.guid for i in first} == {"post-1", "https://example.com/posts/2"}
        assert feed.etag == '"v1"'

        router.get(FEED_URL).respond(200, content=RSS_MORE)
        second = await refresh_feed(session, feed)
        assert [i.guid for i in second] == ["https://example.com/posts/3"]

        third = await refresh_feed(session, feed)
        assert third == []

    stored = await _items(session, feed)
    assert len(stored) == 3
    item = next(i for i in stored if i.guid == "post-1")
    assert item.content_html.startswith("<p>Hello") and item.content_text == "Hello link"
    assert item.published_at.tzinfo is not None
    assert len(item.hash) == 64 and item.enclosure_type == "audio/mpeg"

    calls = [c for c in fake_queue if c[0] == queue.AI_PROCESS_NEW_ITEMS]
    assert len(calls) == 2
    assert calls[1][1] == (str(feed.id), [str(second[0].id)])


async def test_refresh_same_guid_across_feeds_is_not_deduped(session, user, fake_queue):
    a = await _make_feed(session, user, url="https://a.example.com/rss")
    b = await _make_feed(session, user, url="https://b.example.com/rss")
    with _router() as router:
        router.get(url__regex=r"https://[ab]\.example\.com/rss").respond(200, content=RSS)
        assert len(await refresh_feed(session, a)) == 2
        assert len(await refresh_feed(session, b)) == 2


def test_interval_helpers():
    s = get_settings()
    assert shorten_interval(15) == 10
    assert shorten_interval(s.fetch_min_interval_min) == s.fetch_min_interval_min
    assert lengthen_interval(15) == 22 or lengthen_interval(15) == 23
    assert lengthen_interval(s.fetch_max_interval_min) == s.fetch_max_interval_min
    assert backoff_interval(1) == s.fetch_default_interval_min
    assert backoff_interval(2) == s.fetch_default_interval_min * 2
    assert backoff_interval(20) == s.fetch_max_interval_min


async def test_adaptive_interval_shortens_and_lengthens(session, user, fake_queue):
    s = get_settings()
    feed = await _make_feed(session, user, fetch_interval_min=60)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        await refresh_feed(session, feed)
        assert feed.fetch_interval_min == 40
        assert feed.next_fetch_at == feed.last_fetch_at + timedelta(minutes=40)
        # nothing new: lengthen 40 -> 60 -> 90 ...
        await refresh_feed(session, feed)
        assert feed.fetch_interval_min == 60
        await refresh_feed(session, feed)
        assert feed.fetch_interval_min == 90
        for _ in range(40):
            await refresh_feed(session, feed)
        assert feed.fetch_interval_min == s.fetch_max_interval_min
        # new items again: shorten all the way to the floor
        router.get(FEED_URL).respond(200, content=RSS_MORE)
        await refresh_feed(session, feed)
        assert feed.fetch_interval_min == 960
        feed.fetch_interval_min = 6
        router.get(FEED_URL).respond(200, content=ATOM)
        await refresh_feed(session, feed)
        assert feed.fetch_interval_min == s.fetch_min_interval_min


async def test_error_backoff_and_auto_pause(session, user, fake_queue):
    s = get_settings()
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(500)
        assert await refresh_feed(session, feed) == []
        assert feed.error_count == 1 and feed.last_error == "HTTP 500" and feed.paused is False
        assert feed.fetch_interval_min == s.fetch_default_interval_min
        assert feed.last_fetch_at is not None and feed.last_success_at is None
        await refresh_feed(session, feed)
        assert feed.error_count == 2 and feed.fetch_interval_min == s.fetch_default_interval_min * 2
        await refresh_feed(session, feed)
        assert feed.error_count == 3 and feed.fetch_interval_min == s.fetch_default_interval_min * 4

        router.get(FEED_URL).respond(200, content=b"<html><body>not a feed</body></html>")
        await refresh_feed(session, feed)
        assert feed.error_count == 4 and "parse error" in feed.last_error

        router.get(FEED_URL).mock(side_effect=httpx.ConnectError("boom"))
        await refresh_feed(session, feed)
        assert feed.error_count == 5 and "boom" in feed.last_error

        router.get(FEED_URL).respond(500)
        for _ in range(4):
            await refresh_feed(session, feed)
        assert feed.error_count == 9 and feed.paused is False
        await refresh_feed(session, feed)
        assert feed.error_count == 10 and feed.paused is True
        assert feed.fetch_interval_min == s.fetch_max_interval_min

        # a successful fetch resets the counter (paused stays until a user resumes)
        router.get(FEED_URL).respond(200, content=RSS)
        assert len(await refresh_feed(session, feed)) == 2
        assert feed.error_count == 0 and feed.last_error is None and feed.last_success_at is not None

    row = await session.get(models.Feed, feed.id)
    assert row.error_count == 0
    assert fake_queue[-1][0] == queue.AI_PROCESS_NEW_ITEMS


async def test_unsafe_feed_url_is_recorded_as_error(session, user, fake_queue):
    feed = await _make_feed(session, user, url="http://169.254.169.254/latest")
    assert await refresh_feed(session, feed) == []
    assert feed.error_count == 1 and "UnsafeURLError" in feed.last_error


async def test_rules_hide_star_tag_mark_read(session, user, fake_queue):
    feed = await _make_feed(session, user)
    other = await _make_feed(session, user, url="https://other.example.com/rss")
    session.add_all(
        [
            models.FeedRule(user_id=user.id, feed_id=feed.id, field="title", pattern="first", action="hide"),
            models.FeedRule(
                user_id=user.id,
                feed_id=None,
                field="body",
                pattern=r"body\s+two",
                is_regex=True,
                action="star",
            ),
            models.FeedRule(
                user_id=user.id,
                feed_id=None,
                field="url",
                pattern="/posts/2",
                action="tag",
                action_value="two",
            ),
            models.FeedRule(
                user_id=user.id, feed_id=None, field="author", pattern="alice", action="mark_read"
            ),
            models.FeedRule(
                user_id=user.id, feed_id=other.id, field="title", pattern="second", action="hide"
            ),
            models.FeedRule(
                user_id=user.id, feed_id=None, field="title", pattern="second", action="hide", enabled=False
            ),
            models.FeedRule(
                user_id=user.id, feed_id=None, field="title", pattern="[", is_regex=True, action="hide"
            ),
        ]
    )
    await session.commit()
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        items = await refresh_feed(session, feed)
    by_guid = {i.guid: i for i in items}
    states = {
        s.item_id: s
        for s in (
            await session.scalars(select(models.ItemState).where(models.ItemState.user_id == user.id))
        ).all()
    }
    s1 = states[by_guid["post-1"].id]
    assert s1.hidden is True and s1.is_read is True and s1.read_at is not None
    assert s1.is_starred is False and s1.tags == []
    s2 = states[by_guid["https://example.com/posts/2"].id]
    assert s2.is_starred is True and s2.starred_at is not None and s2.tags == ["two"]
    assert s2.hidden is False and s2.is_read is False


async def test_permanent_redirect_updates_feed_url(session, user, fake_queue):
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(301, headers={"Location": "https://example.com/new-feed.xml"})
        router.get("https://example.com/new-feed.xml").respond(200, content=RSS)
        assert len(await refresh_feed(session, feed)) == 2
    assert feed.url == "https://example.com/new-feed.xml"
    row = await session.get(models.Feed, feed.id)
    assert row.url == "https://example.com/new-feed.xml"

    # temporary redirects leave the URL alone
    feed2 = await _make_feed(session, user, url="https://example.com/tmp")
    with _router() as router:
        router.get("https://example.com/tmp").respond(
            302, headers={"Location": "https://example.com/new-feed.xml"}
        )
        router.get("https://example.com/new-feed.xml").respond(200, content=RSS)
        await refresh_feed(session, feed2)
    assert feed2.url == "https://example.com/tmp" and feed2.error_count == 0


async def test_enqueue_failure_does_not_fail_fetch(session, user, monkeypatch):
    async def broken(*a, **kw):
        raise ConnectionError("redis down")

    monkeypatch.setattr("pensieve.queue.enqueue", broken)
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        assert len(await refresh_feed(session, feed)) == 2
    assert feed.error_count == 0


async def test_refresh_fills_missing_metadata(session, user, fake_queue):
    feed = models.Feed(user_id=user.id, url=FEED_URL, title="", next_fetch_at=datetime.now(UTC))
    session.add(feed)
    await session.commit()
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        await refresh_feed(session, feed)
    assert feed.title == "Example & Co" and feed.site_url == "https://example.com/"
    assert feed.websub_hub == "https://hub.example.com/" and feed.icon_url == "https://example.com/logo.png"


async def test_queries_are_user_scoped(session, user, fake_queue):
    """A second user's rules never apply to the first user's feed."""
    other = models.User(email=f"o-{uuid.uuid4().hex[:6]}@example.com", password_hash="x")
    session.add(other)
    await session.commit()
    session.add(models.FeedRule(user_id=other.id, field="title", pattern="post", action="hide"))
    feed = await _make_feed(session, user)
    await session.commit()
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        await refresh_feed(session, feed)
    states = (await session.scalars(select(models.ItemState))).all()
    assert states == []
