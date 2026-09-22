# ruff: noqa: F811  (fixtures imported from test_fetch_helpers are used as test parameters)
import uuid
from datetime import UTC, datetime

import httpx
import respx
from sqlalchemy import select

from pensieve import models
from pensieve.fetch import jobs
from pensieve.fetch.reader_mode import extract_reader_html
from tests.test_fetch_helpers import RSS, fake_queue, no_dns  # noqa: F401

PAGE = b"""<html><head><title>T</title></head><body><nav>menu menu</nav><article><h1>Big story</h1>
<p>This is the first paragraph of a reasonably long article that trafilatura should treat as the main content.</p>
<p>Second paragraph with a <a href="/rel">relative link</a>, an image <img src="/pic.png"> and
<script>alert(1)</script> inline.</p>
<p>Third paragraph to make the body substantial enough for extraction heuristics to be comfortable.</p>
</article></body></html>"""


async def test_extract_reader_html_sanitised():
    with respx.mock() as router:
        router.get("https://example.com/story").respond(
            200, content=PAGE, headers={"content-type": "text/html"}
        )
        html = await extract_reader_html("https://example.com/story")
    assert html is not None
    assert "Big story" in html and "Third paragraph" in html
    assert "script" not in html and "alert" not in html and "menu" not in html
    assert 'href="https://example.com/rel"' in html and 'src="https://example.com/pic.png"' in html
    assert 'target="_blank"' in html


async def test_extract_reader_html_never_raises():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/404").respond(404)
        router.get("https://example.com/boom").mock(side_effect=httpx.ConnectError("x"))
        router.get("https://example.com/pdf").respond(
            200, content=b"%PDF", headers={"content-type": "application/pdf"}
        )
        router.get("https://example.com/empty").respond(
            200, content=b"<html><body></body></html>", headers={"content-type": "text/html"}
        )
        for url in (
            "https://example.com/404",
            "https://example.com/boom",
            "https://example.com/pdf",
            "https://example.com/empty",
        ):
            assert await extract_reader_html(url) is None
    assert await extract_reader_html("http://127.0.0.1/secret") is None
    assert await extract_reader_html("not a url") is None


async def test_fetch_feed_and_reader_mode_jobs(session, user, fake_queue):
    feed = models.Feed(
        user_id=user.id, url="https://example.com/feed.xml", title="t", next_fetch_at=datetime.now(UTC)
    )
    session.add(feed)
    await session.commit()

    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/feed.xml").respond(200, content=RSS)
        router.get("https://example.com/posts/1").respond(
            200, content=PAGE, headers={"content-type": "text/html"}
        )
        router.get("https://example.com/posts/2").respond(500)
        assert await jobs.fetch_feed({}, str(feed.id)) == 2
        assert await jobs.fetch_feed({}, str(uuid.uuid4())) == 0

        items = {
            i.guid: i
            for i in (await session.scalars(select(models.Item).where(models.Item.feed_id == feed.id))).all()
        }
        assert await jobs.fetch_reader_mode({}, str(items["post-1"].id)) is True
        assert await jobs.fetch_reader_mode({}, str(items["https://example.com/posts/2"].id)) is False
        assert await jobs.fetch_reader_mode({}, str(uuid.uuid4())) is False

    ok_id, failed_id, feed_id = items["post-1"].id, items["https://example.com/posts/2"].id, feed.id
    session.expire_all()
    ok = await session.get(models.Item, ok_id)
    assert ok.reader_html and "Big story" in ok.reader_html and ok.reader_fetched_at is not None
    failed = await session.get(models.Item, failed_id)
    assert (
        failed.reader_html is None and failed.reader_fetched_at is not None
    )  # attempted, not retried forever
    row = await session.get(models.Feed, feed_id)
    assert row.last_success_at is not None and row.error_count == 0
    assert fake_queue[0][0] == "ai_process_new_items"


async def test_fetch_due_feeds_and_prune_jobs(session, user, fake_queue):
    feed = models.Feed(user_id=user.id, url="https://example.com/feed.xml", next_fetch_at=datetime.now(UTC))
    session.add(feed)
    await session.commit()
    assert await jobs.fetch_due_feeds({}) == 1
    assert fake_queue[0][2] == f"fetch:{feed.id}"
    assert await jobs.prune_old_items({}) == 0
