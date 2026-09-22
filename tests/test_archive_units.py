# ruff: noqa: F811
"""Pure helpers: URL normalisation, share-text parsing, freezing, exports, and the save/sweep bookkeeping."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pensieve import models
from pensieve.archive import save
from pensieve.archive.freeze import Freezer, collect_refs, css_refs
from pensieve.archive.imports import parse_export
from pensieve.archive.render import Resource
from pensieve.fetch import http as fetch_http
from pensieve.fetch.scheduler import prune_old_items
from tests.test_archive_helpers import PNG
from tests.test_fetch_helpers import fake_queue, no_dns  # noqa: F401


def test_normalize_url_drops_tracking_and_fragments():
    assert save.normalize_url(" example.com/a?utm_source=x&id=3#top ") == "https://example.com/a?id=3"
    assert save.normalize_url("HTTP://Example.COM") == "http://example.com/"
    for bad in ("", "ftp://x.org/f", "javascript:alert(1)"):
        with pytest.raises(save.SaveError):
            save.normalize_url(bad)


def test_find_url_reads_share_sheet_payloads():
    assert save.find_url("", "Look at this https://x.org/post?a=1.", "") == "https://x.org/post?a=1"
    assert save.find_url("https://a.org/b", "text https://c.org") == "https://a.org/b"
    assert save.find_url("", "", "example.com/story") == "example.com/story"
    assert save.find_url("", "no link here", "") is None


def test_freeze_strips_active_content_and_inlines_styles():
    base = "https://s.example.com/p/1"
    resources = {
        "https://s.example.com/a.css": Resource(
            "https://s.example.com/a.css",
            "text/css",
            b"@import url(b.css) screen; .x{background:url('../img/bg.png')}",
        ),
        "https://s.example.com/b.css": Resource(
            "https://s.example.com/b.css", "text/css", b".b{color:blue;src:url(f.woff2)}"
        ),
        "https://s.example.com/img/bg.png": Resource("https://s.example.com/img/bg.png", "image/png", PNG),
        "https://s.example.com/f.woff2": Resource("https://s.example.com/f.woff2", "font/woff2", b"wOF2..."),
        "https://s.example.com/p/pic.jpg": Resource(
            "https://s.example.com/p/pic.jpg", "image/jpeg", b"\xff\xd8jpeg"
        ),
    }
    dom = """<html><head><link rel="stylesheet" href="/a.css"><script>x()</script><meta http-equiv="refresh" content="0;url=/">
    <base href="https://evil.example/"></head><body onclick="y()"><img src="pic.jpg" srcset="pic-2x.jpg 2x">
    <img src="missing.jpg"><a href="rel/link">rel</a><form action="/login"><input name="q" onfocus="z()"></form>
    <svg><script>alert(1)</script></svg><object data="x.swf"></object><div style="background:url(/img/bg.png)">d</div>
    </body></html>"""
    f = Freezer(resources)
    out = f.freeze(dom, base, original_url=base, captured_at=datetime(2026, 9, 23, tzinfo=UTC))
    for bad in (
        "<script",
        "onclick",
        "onfocus",
        "http-equiv",
        "<base",
        "<object",
        'action="/login"',
        "srcset",
    ):
        assert bad not in out, bad
    assert ".b{color:blue" in out and "@media screen" in out  # @import inlined with its media query
    assert 'href="https://s.example.com/p/rel/link"' in out and 'target="_blank"' in out
    assert out.count("/archive/a/") >= 4  # jpg, css background (x2) and font
    assert 'data-original-src="https://s.example.com/p/missing.jpg"' in out  # never loads from the network
    assert len(f.used) == 3


def test_collect_refs_finds_what_the_browser_missed():
    dom = '<html><head><link rel="stylesheet" href="/s.css"><style>@import "t.css"; a{b:url(i.png)}</style></head><body><img src="p.png"><div style="background:url(q.png)"></div></body></html>'
    wanted = collect_refs(dom, "https://e.org/x/", {"https://e.org/x/p.png": Resource("", "image/png", b"")})
    assert set(wanted) == {
        "https://e.org/s.css",
        "https://e.org/x/t.css",
        "https://e.org/x/i.png",
        "https://e.org/x/q.png",
    }
    assert css_refs("a{b:url(data:image/png;base64,xx)}", "https://e.org/") == ([], [])


def test_parse_pocket_instapaper_and_bookmarks_exports():
    pocket_html = b"""<!DOCTYPE html><html><body><h1>Unread</h1><ul>
    <li><a href="https://a.org/1" time_added="1700000000" tags="rust,tools">One</a></li></ul>
    <h1>Read Archive</h1><ul><li><a href="https://a.org/2" time_added="1600000000" tags="">Two</a></li></ul></body></html>"""
    links = {link.url: link for link in parse_export("ril_export.html", pocket_html)}
    assert links["https://a.org/1"].tags == ["rust", "tools"] and not links["https://a.org/1"].read
    assert links["https://a.org/2"].read and links["https://a.org/2"].saved_at == 1600000000
    pocket_csv = b"title,url,time_added,tags,status\nOne,https://b.org/1,1700000000,a|b,unread\nTwo,https://b.org/2,1700000001,,archive\n"
    links = {link.url: link for link in parse_export("part_000000.csv", pocket_csv)}
    assert links["https://b.org/1"].tags == ["a", "b"] and links["https://b.org/2"].read
    insta = b"URL,Title,Selection,Folder,Timestamp\nhttps://c.org/1,C,,Unread,1700000000\nhttps://c.org/2,D,,Archive,1700000002\nhttps://c.org/3,E,,Starred,1\n"
    links = {link.url: link for link in parse_export("instapaper-export.csv", insta)}
    assert (
        not links["https://c.org/1"].read
        and links["https://c.org/2"].read
        and links["https://c.org/3"].starred
    )
    netscape = b'<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><DT><A HREF="https://d.org/" ADD_DATE="1700000000" TAGS="x">D</A><DT><A HREF="place:x">no</A></DL>'
    assert [link.url for link in parse_export("bookmarks.html", netscape)] == ["https://d.org/"]


async def test_save_link_is_idempotent_and_scoped(session, user, fake_queue):
    first = await save.save_link(
        session,
        user,
        "https://example.com/a?utm_medium=rss",
        title="A",
        tags=["Rust", " rust "],
        note="for later",
    )
    assert first.created and first.item.url == "https://example.com/a" and first.item.title == "A"
    feed = await session.get(models.Feed, first.item.feed_id)
    assert feed.kind == models.FEED_KIND_SAVED and feed.paused and feed.next_fetch_at is None
    state = await session.get(models.ItemState, (user.id, first.item.id))
    assert state.tags == ["rust"] and not state.is_read
    await session.execute(models.ItemState.__table__.update().values(is_read=True))
    await session.commit()
    again = await save.save_link(session, user, "https://example.com/a", tags=["later"])
    assert not again.created and again.item.id == first.item.id
    await session.refresh(state)
    assert state.tags == ["later", "rust"] and not state.is_read  # back on My list
    assert (
        await session.scalar(select(func.count()).select_from(models.Feed).where(models.Feed.kind == "saved"))
        == 1
    )
    assert await session.scalar(select(func.count()).select_from(models.Note)) == 1
    # Still queued: re-saving hands arq the same job id again (it keeps one), never a second capture.
    assert {c[2] for c in fake_queue if c[0] == "capture_page"} == {f"capture_page:{first.snapshot.id}"}
    first.snapshot.status = "done"
    await session.commit()
    fake_queue.clear()
    await save.save_link(session, user, "https://example.com/a")
    assert fake_queue == []  # a finished capture is kept on re-save


async def test_save_link_refuses_private_addresses(session, user, fake_queue, monkeypatch):
    monkeypatch.setattr(fetch_http, "resolve_host", lambda host: ["10.0.1.100"])
    with pytest.raises(save.SaveError):
        await save.save_link(session, user, "https://intranet.example.com/")
    with pytest.raises(save.SaveError):
        await save.save_link(session, user, "http://127.0.0.1:8000/admin")
    assert fake_queue == []


async def test_saved_links_survive_retention_and_join_the_history_pool(session, user, fake_queue):
    from pensieve.ai.memory import _history_pool

    result = await save.save_link(
        session, user, "https://example.com/old", saved_at=datetime.now(UTC) - timedelta(days=3000)
    )
    assert result.item.published_at < datetime.now(UTC) - timedelta(days=2000)
    assert await prune_old_items(session) == 0
    assert result.item.id in await _history_pool(session, user.id)  # unread, but deliberately saved


async def test_sweep_trickles_waiting_captures(session, user, fake_queue):
    for n in range(5):
        await save.save_link(session, user, f"https://example.com/{n}", capture=False)
    old = datetime.now(UTC) - timedelta(minutes=30)
    table = models.Snapshot.__table__
    await session.execute(table.update().values(updated_at=old))
    stuck_id = await session.scalar(select(models.Snapshot.id).limit(1))
    await session.execute(
        table.update().where(table.c.id == stuck_id).values(status="rendering", attempts=3, updated_at=old)
    )
    await session.commit()
    retry, failed = await save.stale_captures(session, room=2)
    assert failed == 1 and len(retry) == 2
    retry2, _ = await save.stale_captures(session, room=10)
    assert len(retry2) == 2 and not set(retry) & set(retry2)  # handed-out rows wait their turn


def test_extract_prefers_rendered_and_keeps_short_pages():
    from pensieve.archive.extract import extract_best

    short = "<html><head><title>Tiny</title></head><body><main><h1>Tiny</h1><p>This domain is for use in documentation examples without asking.</p></main></body></html>"
    got = extract_best(short, None, "https://tiny.example/")
    assert got is not None and "documentation examples" in got.text
    long_text = "<p>" + "Words about orchards and apples. " * 40 + "</p>"
    rendered = f"<html><body><article><h1>T</h1>{long_text}</article></body></html>"
    raw = "<html><body><div id=root></div></body></html>"
    assert extract_best(rendered, raw, "https://x.example/").source == "rendered"
    assert extract_best(raw, rendered, "https://x.example/").source == "raw"  # a JS wall hid the body
    assert extract_best("<html><body></body></html>", None, "https://x.example/") is None
