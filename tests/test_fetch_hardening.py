# ruff: noqa: F811  (fixtures imported from test_fetch_helpers are used as test parameters)
"""Fetch hardening: time-ordered ids, refresh_feed robustness, HTTP caps, DNS pinning, claim scheduling,
hash dedupe, the sanitiser's iframe/srcset rules and favicon caching."""

import asyncio
import types
from datetime import UTC, datetime, timedelta

import httpcore
import httpx
import pytest
import respx
from sqlalchemy import select

from pensieve import models, queue
from pensieve.config import get_settings
from pensieve.fetch import http as fetch_http
from pensieve.fetch import scheduler
from pensieve.fetch.favicon import fetch_favicon, refresh_feed_icon
from pensieve.fetch.parse import clean_srcset, sanitize_html
from pensieve.fetch.subscribe import ingest, parse_retry_after, refresh_feed
from pensieve.syncapi.common import long_id
from tests.test_fetch_helpers import (
    PUBLIC_IP,
    RSS,
    RSS_MORE,
    fake_queue,  # noqa: F401
    no_dns,  # noqa: F401
)
from tests.test_fetch_subscribe import FEED_URL, _items, _make_feed, _router

# ---------------------------------------------------------------------------------------------- ids


async def test_item_ids_are_time_ordered(session, user, fake_queue, monkeypatch):
    """Items ingested a second apart get UUIDv7 ids whose long_id (sync API int64) increases with time."""
    base = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    clock = {"ns": int(base.timestamp() * 1_000_000_000)}
    monkeypatch.setattr(models, "time", types.SimpleNamespace(time_ns=lambda: clock["ns"]))
    a = await _make_feed(session, user, url="https://a.example.com/rss")
    b = await _make_feed(session, user, url="https://b.example.com/rss")
    with _router() as router:
        router.get("https://a.example.com/rss").respond(200, content=RSS)
        first = await refresh_feed(session, a)
        clock["ns"] += 1_000_000_000  # one second later
        router.get("https://b.example.com/rss").respond(200, content=RSS)
        second = await refresh_feed(session, b)
    assert len(first) == 2 and len(second) == 2
    assert all(i.id.version == 7 for i in first + second)
    assert max(long_id(i.id) for i in first) < min(long_id(i.id) for i in second)


def test_item_ids_are_unique_and_monotonic_within_one_millisecond(monkeypatch):
    monkeypatch.setattr(models, "time", types.SimpleNamespace(time_ns=lambda: 1_800_000_000_000_000_000))
    monkeypatch.setattr(models, "_uuid7_last_ms", -1)
    monkeypatch.setattr(models, "_uuid7_sequence", 0)

    ids = [long_id(models._uuid7()) for _ in range(4097)]

    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------------------------- refresh_feed


async def test_unexpected_exception_is_recorded_not_raised(session, user, fake_queue, monkeypatch):
    feed = await _make_feed(session, user)

    async def boom(*a, **kw):
        raise RuntimeError("uq_item_feed_guid race")

    monkeypatch.setattr("pensieve.fetch.subscribe.ingest", boom)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        assert await refresh_feed(session, feed) == []
    row = await session.get(models.Feed, feed.id)
    assert row.error_count == 1 and "RuntimeError" in row.last_error and "race" in row.last_error
    assert row.next_fetch_at > datetime.now(UTC)


async def test_permanent_redirect_truncates_url_and_clears_validators(session, user, fake_queue):
    feed = await _make_feed(session, user, etag='"old"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
    long_target = "https://example.com/" + "x" * 2100
    with _router() as router:
        router.get(FEED_URL).respond(301, headers={"Location": long_target})
        router.get(url__regex=r"https://example\.com/x+").respond(200, content=RSS, headers={"ETag": '"new"'})
        assert len(await refresh_feed(session, feed)) == 2
    assert len(feed.url) == 2048 and feed.url.startswith("https://example.com/xxx")
    assert feed.etag == '"new"'  # validators from the new resource, not the old one


async def test_retry_after_seconds_and_http_date(session, user, fake_queue):
    s = get_settings()
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(429, headers={"Retry-After": "1800"})
        assert await refresh_feed(session, feed) == []
    assert feed.error_count == 1 and feed.last_error == "HTTP 429"
    assert feed.fetch_interval_min == 30
    assert abs((feed.next_fetch_at - feed.last_fetch_at) - timedelta(minutes=30)) < timedelta(seconds=1)

    when = datetime.now(UTC) + timedelta(hours=2)
    with _router() as router:
        router.get(FEED_URL).respond(503, headers={"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S GMT")})
        await refresh_feed(session, feed)
    assert feed.error_count == 2 and feed.last_error == "HTTP 503"
    assert timedelta(minutes=118) < feed.next_fetch_at - feed.last_fetch_at <= timedelta(hours=2)

    # A silly Retry-After is clamped to the configured bounds; a missing one falls back to backoff.
    with _router() as router:
        router.get(FEED_URL).respond(429, headers={"Retry-After": "999999999"})
        await refresh_feed(session, feed)
    assert feed.next_fetch_at - feed.last_fetch_at == timedelta(minutes=s.fetch_max_interval_min)
    with _router() as router:
        router.get(FEED_URL).respond(503)
        await refresh_feed(session, feed)
    assert feed.error_count == 4 and feed.fetch_interval_min == s.fetch_default_interval_min * 8

    now = datetime.now(UTC)
    assert parse_retry_after("120", now) == timedelta(seconds=120)
    assert parse_retry_after("garbage", now) is None
    assert parse_retry_after(None, now) is None
    assert parse_retry_after((now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT"), now) == timedelta(0)


async def test_gone_pauses_immediately(session, user, fake_queue):
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(410)
        assert await refresh_feed(session, feed) == []
    assert feed.paused is True and feed.error_count == 1 and feed.last_error == "Feed is gone (410)"
    row = await session.get(models.Feed, feed.id)
    assert row.paused is True


async def test_304_copies_validators_when_present(session, user, fake_queue):
    feed = await _make_feed(session, user, etag='"e1"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
    with _router() as router:
        router.get(FEED_URL).respond(304, headers={"ETag": '"e2"'})
        await refresh_feed(session, feed)
    assert feed.etag == '"e2"' and feed.last_modified is None
    with _router() as router:
        router.get(FEED_URL).respond(304)  # nothing sent: keep what we have
        await refresh_feed(session, feed)
    assert feed.etag == '"e2"'


# ---------------------------------------------------------------------------------------------- HTTP caps


async def test_body_over_max_bytes_is_abandoned(monkeypatch):
    monkeypatch.setattr(get_settings(), "fetch_max_bytes", 1000)
    with respx.mock() as router:
        router.get("https://big.example.com/feed").respond(200, content=b"x" * 2000)
        with pytest.raises(fetch_http.ResponseTooLarge):
            await fetch_http.get("https://big.example.com/feed")
        router.get("https://declared.example.com/feed").respond(
            200, content=b"x" * 10, headers={"Content-Length": "5000000"}
        )
        with pytest.raises(fetch_http.ResponseTooLarge):
            await fetch_http.get("https://declared.example.com/feed")
        router.get("https://small.example.com/feed").respond(200, content=b"y" * 999)
        response = await fetch_http.get("https://small.example.com/feed")
    assert response.content == b"y" * 999 and response.status_code == 200


async def test_oversized_feed_is_recorded_as_error(session, user, fake_queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "fetch_max_bytes", 100)
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        assert await refresh_feed(session, feed) == []
    assert feed.error_count == 1 and "ResponseTooLarge" in feed.last_error


async def test_total_timeout_bounds_a_slow_server(monkeypatch):
    monkeypatch.setattr(get_settings(), "fetch_total_timeout_s", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, text="late")

    with respx.mock(assert_all_called=False) as router:  # cancelled mid-flight: respx never marks it called
        router.get("https://slow.example.com/").mock(side_effect=slow)
        with pytest.raises(TimeoutError):
            await fetch_http.get("https://slow.example.com/")


async def test_timeout_is_recorded_as_error(session, user, fake_queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "fetch_total_timeout_s", 0.05)
    feed = await _make_feed(session, user)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, content=RSS)

    with _router() as router:
        router.get(FEED_URL).mock(side_effect=slow)
        assert await refresh_feed(session, feed) == []
    assert feed.error_count == 1 and "TimeoutError" in feed.last_error


# ---------------------------------------------------------------------------------------------- DNS pinning


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        raise httpcore.ConnectError("test backend never connects")


async def test_pinned_backend_connects_to_validated_ip_not_hostname(monkeypatch):
    inner = _RecordingBackend()
    backend = fetch_http.PinnedBackend(inner)
    # Addresses recorded by the request hook win; no second lookup happens.
    token = fetch_http._validated_addresses.set({"feed.example.com": ["93.184.216.34"]})
    try:
        monkeypatch.setattr(fetch_http, "resolve_host", lambda host: pytest.fail("must not resolve again"))
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("feed.example.com", 443)
    finally:
        fetch_http._validated_addresses.reset(token)
    assert inner.hosts == ["93.184.216.34"]


async def test_pinned_backend_refuses_rebound_private_address(monkeypatch):
    inner = _RecordingBackend()
    backend = fetch_http.PinnedBackend(inner)
    # Nothing pinned (client used without the hook): the backend resolves and validates itself.
    monkeypatch.setattr(fetch_http, "resolve_host", lambda host: ["10.0.0.7"])
    with pytest.raises(httpcore.ConnectError, match="non-public"):
        await backend.connect_tcp("rebinder.example.com", 80)
    assert inner.hosts == []
    monkeypatch.setattr(fetch_http, "resolve_host", lambda host: [PUBLIC_IP])
    with pytest.raises(httpcore.ConnectError, match="never connects"):
        await backend.connect_tcp("rebinder.example.com", 80)
    assert inner.hosts == [PUBLIC_IP]
    # IP literals pass straight through (already validated by the hook).
    with pytest.raises(httpcore.ConnectError, match="never connects"):
        await backend.connect_tcp("93.184.216.34", 80)
    assert inner.hosts[-1] == "93.184.216.34"


async def test_request_hook_pins_addresses_for_the_backend(monkeypatch):
    monkeypatch.setattr(fetch_http, "resolve_host", lambda host: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
    seen: dict = {}

    async def capture(request: httpx.Request) -> httpx.Response:
        seen.update(fetch_http._validated_addresses.get() or {})
        return httpx.Response(200, text="ok")

    with respx.mock() as router:
        router.get("https://pin.example.com/feed").mock(side_effect=capture)
        await fetch_http.get("https://pin.example.com/feed")
    assert seen == {"pin.example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]}


def test_client_uses_pinned_transport():
    client = fetch_http.get_client()
    assert isinstance(client._transport, fetch_http.PinnedTransport)
    assert isinstance(client._transport._pool._network_backend, fetch_http.PinnedBackend)


# ---------------------------------------------------------------------------------------------- scheduler claim


async def test_claim_due_feeds_is_atomic_and_pushes_next_fetch(session, user, fake_queue):
    now = datetime.now(UTC)
    feeds = [
        models.Feed(user_id=user.id, url=f"https://{n}.example.com/rss", next_fetch_at=now - timedelta(minutes=n))
        for n in range(1, 4)
    ]
    later = models.Feed(user_id=user.id, url="https://later.example.com/rss", next_fetch_at=now + timedelta(hours=1))
    session.add_all([*feeds, later])
    await session.commit()

    claimed = await scheduler.claim_due_feeds(session, now, limit=2)
    assert set(claimed) == {feeds[2].id, feeds[1].id}  # the two most overdue (RETURNING order is unspecified)
    for f in feeds[1:]:
        await session.refresh(f)
        assert f.next_fetch_at == now + scheduler.CLAIM_HOLD
    await session.refresh(feeds[0])
    assert feeds[0].next_fetch_at < now  # not claimed this round
    # The claimed feeds are no longer due; the remaining one is picked up next.
    assert await scheduler.claim_due_feeds(session, now) == [feeds[0].id]
    assert await scheduler.claim_due_feeds(session, now) == []
    assert await scheduler.enqueue_due_feeds(session, now) == 0
    await session.refresh(later)
    assert later.next_fetch_at == now + timedelta(hours=1)


async def test_enqueue_due_feeds_enqueues_only_claimed_ids(session, user, fake_queue):
    now = datetime.now(UTC)
    due = models.Feed(user_id=user.id, url="https://a.example.com/rss", next_fetch_at=now - timedelta(minutes=1))
    session.add(due)
    await session.commit()
    assert await scheduler.enqueue_due_feeds(session, now) == 1
    assert fake_queue == [(queue.FETCH_FEED, (str(due.id),), queue.job_id_for("fetch", due.id))]
    assert await scheduler.enqueue_due_feeds(session, now) == 0  # claimed: not enqueued twice
    assert len(fake_queue) == 1


# ---------------------------------------------------------------------------------------------- hash dedupe


async def test_ingest_skips_same_hash_under_new_guid_and_updates_changed_content(session, user, fake_queue):
    feed = await _make_feed(session, user)
    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        first = await refresh_feed(session, feed)
        assert len(first) == 2
        # Same two articles re-published under new guids (a CMS migration): nothing new.
        regu = RSS.replace(b"post-1", b"post-1-new").replace(b"https://example.com/posts/2</link>", b"https://example.com/posts/2b</link>")
        router.get(FEED_URL).respond(200, content=regu)
        assert await refresh_feed(session, feed) == []
        assert len(await _items(session, feed)) == 2
        # The known guid post-1 now carries edited content: updated in place, not duplicated.
        edited = RSS.replace(b"<p>Hello ", b"<p>Hello again ")
        router.get(FEED_URL).respond(200, content=edited)
        assert await refresh_feed(session, feed) == []
    items = {i.guid: i for i in await _items(session, feed)}
    assert len(items) == 2
    post1 = items["post-1"]
    await session.refresh(post1)
    assert post1.content_text == "Hello again link" and post1.updated_at is not None
    assert items["https://example.com/posts/2"].updated_at is None
    # Only the first fetch enqueued AI processing.
    assert [c[0] for c in fake_queue] == [queue.AI_PROCESS_NEW_ITEMS]
    assert fake_queue[0][2] is None  # no fixed job id: a second batch must never be dropped


async def test_ingest_dedupes_hash_within_one_batch(session, user, fake_queue):
    feed = await _make_feed(session, user)
    from pensieve.fetch.parse import parse_feed

    doubled = RSS.replace(b"<guid isPermaLink=\"false\">post-1</guid>", b"<guid isPermaLink=\"false\">post-1</guid>")
    parsed = parse_feed(doubled, FEED_URL)
    dup = parsed.entries[0]
    parsed.entries.append(type(dup)(**{f: getattr(dup, f) for f in dup.__slots__}))
    parsed.entries[-1].guid = "post-1-mirror"
    items = await ingest(session, feed, parsed)
    assert sorted(i.guid for i in items) == ["https://example.com/posts/2", "post-1"]


# ---------------------------------------------------------------------------------------------- sanitiser


def test_sanitizer_srcset_and_iframe_and_svg_and_data_href():
    base = "https://site.example/post/"
    out = sanitize_html(
        '<img src="/a.png" srcset="/a.png 1x, /b.png 2x, javascript:alert(1) 3x, data:image/png;base64,AAAA 4x">',
        base_url=base,
    )
    assert out == (
        '<img src="https://site.example/a.png" srcset="https://site.example/a.png 1x, '
        'https://site.example/b.png 2x, data:image/png;base64,AAAA 4x">'
    )
    out = sanitize_html('<picture><source srcset="/w.webp 640w, data:text/html,x 1x" type="image/webp"><img src="/f.jpg"></picture>', base_url=base)
    assert 'srcset="https://site.example/w.webp 640w"' in out and "data:" not in out
    assert sanitize_html('<img srcset="javascript:x 1x, data:text/plain,y 2x">') == "<img>"
    assert clean_srcset("https://a/b.png 1x,https://a/c.png 2x") == "https://a/b.png 1x, https://a/c.png 2x"

    out = sanitize_html('<p>x</p><svg onload="alert(1)"><circle r="1"/></svg><p>y</p>')
    assert "onload" not in out and "svg" not in out and out == "<p>x</p><p>y</p>"

    out = sanitize_html('<a href="data:text/html,<script>alert(1)</script>">x</a>')
    assert "data:" not in out and "href" not in out and ">x</a>" in out


def test_sanitizer_iframe_allowlist_and_prestrip():
    yt = '<p><iframe src="https://www.youtube.com/embed/abc" width="560" height="315" allowfullscreen></iframe></p>'
    out = sanitize_html(yt)
    assert 'src="https://www.youtube.com/embed/abc"' in out and 'sandbox="allow-scripts' in out and 'loading="lazy"' in out
    assert "<iframe" in sanitize_html('<iframe src="https://player.vimeo.com/video/1"></iframe>')
    assert "<iframe" in sanitize_html('<iframe src="https://www.youtube-nocookie.com/embed/x"></iframe>')
    # Unknown hosts, http embeds and srcdoc-only frames are dropped entirely (text inside too).
    assert sanitize_html('<p>a<iframe src="https://evil.example/x">junk</iframe>b</p>') == "<p>ab</p>"
    assert sanitize_html('<p>a<iframe src="http://www.youtube.com/embed/x"></iframe>b</p>') == "<p>ab</p>"
    assert sanitize_html('<p>x<iframe srcdoc="<script>alert(1)</script>"></iframe></p>') == "<p>x</p>"
    assert "youtube.com.evil.example" not in sanitize_html('<iframe src="https://www.youtube.com.evil.example/e"></iframe>')
    # Mixed case and unclosed: the pre-strip regex removes the open tag; the rest of the document survives.
    out = sanitize_html("<p>a<IFRAME SRC='http://evil.example/x'>after</p><p>more</p>")
    assert out == "<p>aafter</p><p>more</p>" and "iframe" not in out.lower()
    out = sanitize_html('<p><IFRAME src="https://www.youtube.com/embed/ok"></p>')
    assert out.count("<iframe") == 1 and "</iframe>" in out


# ---------------------------------------------------------------------------------------------- favicons


async def test_favicon_fetch_and_add_feed_caches_bytes(session, user, fake_queue):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    feed = models.Feed(user_id=user.id, url=FEED_URL, site_url="https://example.com/", icon_url="https://example.com/logo.png")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/logo.png").respond(200, content=png, headers={"content-type": "image/png"})
        found = await fetch_favicon(feed)
    assert found == (png, "image/png")

    # icon_url dead -> /favicon.ico fallback; a non-image body is rejected.
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/logo.png").respond(404)
        router.get("https://example.com/favicon.ico").respond(200, content=b"<html>nope</html>", headers={"content-type": "text/html"})
        assert await fetch_favicon(feed) is None
        router.get("https://example.com/favicon.ico").respond(200, content=b"\x00\x00\x01\x00" + b"\x00" * 8)
        found = await fetch_favicon(feed)
    assert found is not None and found[1] == "image/x-icon"

    session.add(feed)
    await session.commit()
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/logo.png").respond(200, content=png, headers={"content-type": "image/png"})
        assert await refresh_feed_icon(session, feed)
    await session.commit()
    row = await session.scalar(select(models.Feed).where(models.Feed.id == feed.id))
    assert bytes(row.icon_data) == png and row.icon_content_type == "image/png"

    # add_feed caches the icon right after subscribing.
    from pensieve.fetch.subscribe import add_feed

    with _router() as router:
        router.get("https://new.example.com/feed.xml").respond(200, content=RSS_MORE)
        router.get("https://example.com/logo.png").respond(200, content=png, headers={"content-type": "image/png"})
        added = await add_feed(session, user, "https://new.example.com/feed.xml")
    assert bytes(added.icon_data) == png


async def test_favicon_failures_never_break_subscribe(session, user, fake_queue, monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("icon host exploded")

    monkeypatch.setattr("pensieve.fetch.favicon.fetch_favicon", broken)
    from pensieve.fetch.subscribe import add_feed

    with _router() as router:
        router.get(FEED_URL).respond(200, content=RSS)
        feed = await add_feed(session, user, FEED_URL)
    assert feed.icon_data is None and feed.id is not None


async def test_opml_import_enqueues_filing_for_unfiled_feeds(session, user, fake_queue):
    from pensieve.fetch.opml import import_opml

    opml = b"""<opml version="2.0"><body>
      <outline text="Tech"><outline type="rss" xmlUrl="https://t.example.com/rss"/></outline>
      <outline type="rss" xmlUrl="https://loose.example.com/rss"/>
    </body></opml>"""
    result = await import_opml(session, user, opml)
    loose = next(f for f in result.added if f.folder_id is None)
    assert fake_queue == [(queue.AI_FILE_FEED, (str(loose.id),), queue.job_id_for(queue.AI_FILE_FEED, loose.id))]


# ---------------------------------------------------------------------------------------------- gzip


async def test_gzip_body_is_decoded_exactly_once(no_dns):
    """The capped reader returns decoded bytes; copying content-encoding onto the rebuilt response would
    make httpx inflate them a second time ("Error -3 ... incorrect header check" on every gzip feed)."""
    import gzip

    with _router() as router:
        router.get(FEED_URL).respond(200, content=gzip.compress(RSS), headers={"Content-Encoding": "gzip"})
        resp = await fetch_http.get(FEED_URL)
    assert resp.status_code == 200
    assert resp.content == RSS
    assert "content-encoding" not in resp.headers
