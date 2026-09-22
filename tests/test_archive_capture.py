# ruff: noqa: F811  (fixtures imported from helper modules are used as test parameters)
"""Capture pipeline: raw fetch + rendered DOM -> article, frozen page, screenshot, assets; fallbacks."""

import gzip
from datetime import UTC, datetime

import respx
from sqlalchemy import func, select

from pensieve import models, queue
from pensieve.archive import capture, save
from pensieve.archive.render import RenderError
from pensieve.archive.storage import asset_key
from tests.test_archive_helpers import (  # noqa: F401
    JPEG,
    PNG,
    RENDERED_DOM,
    SPA_SHELL,
    FakeRenderer,
    browser,
    bucket,
    no_bucket,
    rendered,
)
from tests.test_fetch_helpers import fake_queue, no_dns  # noqa: F401

URL = "https://news.example.com/story"


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF whose content stream draws ``text`` (pypdf can extract it)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


async def _saved(session, user, fake_queue, url=URL, **kw):
    result = await save.save_link(session, user, url, **kw)
    return result.item, result.snapshot


async def test_capture_rendered_page_end_to_end(session, user, fake_queue, bucket, browser):
    item, snap = await _saved(session, user, fake_queue)
    assert snap.status == "queued"
    assert fake_queue[-1][0] == queue.CAPTURE_PAGE and fake_queue[-1][2] == f"{queue.CAPTURE_PAGE}:{snap.id}"
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(200, text=RENDERED_DOM, headers={"content-type": "text/html; charset=utf-8"})
        router.get(url__regex=r".*").respond(404)
        done = await capture.capture_snapshot(session, snap.id)
    assert done.status == "done" and done.render_mode == "browser" and done.error is None
    assert browser.calls == [URL]
    await session.refresh(item)
    # The saved item now carries the full article: search and every AI step read content_text.
    assert "orchard survey counted every apple tree" in item.content_text.lower()
    assert item.title in {"Orchard survey finds a record harvest", "Orchard survey"}
    assert done.site_name == "Valley Gazette" and done.word_count > 50
    # Blobs: raw HTML and frozen page are gzip, screenshot as-is, images as shared assets.
    raw = gzip.decompress(bucket.objects[done.raw_key][0]).decode()
    page = gzip.decompress(bucket.objects[done.page_key][0]).decode()
    assert "window.track" in raw
    assert bucket.objects[done.shot_key][0] == JPEG
    for bad in ("<script", "onload=", "onclick=", "javascript:", "<iframe", 'rel="preload"', "site.css"):
        assert bad not in page, bad
    assert "archive-banner" in page and "Archived copy of" in page
    assert "Embedded content: video.example.com" in page
    assert "/archive/a/" in page and "h1{color:red}" in page  # stylesheet inlined, its background rewritten
    assets = set(
        (
            await session.scalars(
                select(models.SnapshotAsset.sha256).where(models.SnapshotAsset.snapshot_id == done.id)
            )
        ).all()
    )
    assert len(assets) >= 3 and all(asset_key(sha) in bucket.objects for sha in assets)
    assert done.lead_image_sha in assets
    assert "/archive/a/" in item.content_html  # the article's image points at the archived copy
    assert done.bytes_total > 0
    # The AI pipeline is told about it like a freshly fetched feed item.
    assert any(call[0] == queue.AI_PROCESS_NEW_ITEMS and call[1][1] == [str(item.id)] for call in fake_queue)
    # Full-text search finds words from deep in the page.
    hits = await session.scalar(
        select(func.count())
        .select_from(models.Item)
        .where(
            models.Item.id == item.id,
            models.Item.search_vector.op("@@")(func.websearch_to_tsquery("english", "heritage graft")),
        )
    )
    assert hits == 1


async def test_recapture_replaces_blobs_and_reuses_assets(session, user, fake_queue, bucket, browser):
    item, snap = await _saved(session, user, fake_queue)
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(200, text=RENDERED_DOM, headers={"content-type": "text/html"})
        router.get(url__regex=r".*").respond(404)
        first = await capture.capture_snapshot(session, snap.id)
        old_page = first.page_key
        await save.ensure_snapshot(session, user.id, item, URL, force=True)
        await session.commit()
        second = await capture.capture_snapshot(session, snap.id)
    assert second.generation == 2 and second.page_key != old_page
    assert old_page not in bucket.objects and second.page_key in bucket.objects
    assert await session.scalar(select(func.count()).select_from(models.ArchiveAsset)) == len(
        [k for k in bucket.objects if k.startswith("assets/")]
    )


async def test_client_rendered_page_uses_the_browser_dom(session, user, fake_queue, bucket, browser):
    """The server only sends an app shell; the article exists only after scripts ran."""
    item, snap = await _saved(session, user, fake_queue)
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(200, text=SPA_SHELL, headers={"content-type": "text/html"})
        router.get(url__regex=r".*").respond(404)
        done = await capture.capture_snapshot(session, snap.id)
    await session.refresh(item)
    assert done.status == "done" and "apple tree" in item.content_text


async def test_browser_failure_falls_back_to_server_html(session, user, fake_queue, bucket):
    from pensieve.archive import render as render_mod

    render_mod.set_renderer(FakeRenderer(RenderError("browser unavailable")))
    try:
        item, snap = await _saved(session, user, fake_queue)
        with respx.mock(assert_all_called=False) as router:
            router.get(URL).respond(200, text=RENDERED_DOM, headers={"content-type": "text/html"})
            router.get("https://news.example.com/pic.png").respond(
                200, content=PNG, headers={"content-type": "image/png"}
            )
            router.get(url__regex=r".*").respond(404)
            done = await capture.capture_snapshot(session, snap.id)
    finally:
        render_mod.set_renderer(None)
    await session.refresh(item)
    assert done.status == "done" and done.render_mode == "http" and done.shot_key is None
    assert "render failed" in (done.error or "")
    assert "apple tree" in item.content_text
    assert "/archive/a/" in item.content_html  # the image was fetched directly instead


async def test_pdf_is_kept_as_a_file_with_its_text(session, user, fake_queue, bucket, browser):
    item, snap = await _saved(session, user, fake_queue, url="https://papers.example.com/report.pdf")
    with respx.mock() as router:
        router.get("https://papers.example.com/report.pdf").respond(
            200,
            content=_pdf("Quarterly orchard yields rose sharply"),
            headers={"content-type": "application/pdf"},
        )
        done = await capture.capture_snapshot(session, snap.id)
    await session.refresh(item)
    assert done.render_mode == "file" and done.file_key in bucket.objects and browser.calls == []
    assert "orchard yields" in item.content_text and item.title.startswith("report.pdf")


async def test_nothing_fetchable_marks_the_capture_failed(session, user, fake_queue, bucket):
    from pensieve.archive import render as render_mod

    render_mod.set_renderer(FakeRenderer(RenderError("down")))
    try:
        item, snap = await _saved(session, user, fake_queue)
        with respx.mock() as router:
            router.get(URL).respond(503)
            done = await capture.capture_snapshot(session, snap.id)
    finally:
        render_mod.set_renderer(None)
    assert done.status == "failed" and "503" in done.error and "render failed" in done.error
    await session.refresh(item)
    assert item.title == URL  # still a bookmark; Retry is offered


async def test_text_only_mode_never_points_at_missing_assets(session, user, fake_queue, no_bucket, browser):
    item, snap = await _saved(session, user, fake_queue)
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(200, text=RENDERED_DOM, headers={"content-type": "text/html"})
        done = await capture.capture_snapshot(session, snap.id)
    await session.refresh(item)
    assert done.status == "done" and done.page_key is None and done.shot_key is None
    assert "/archive/a/" not in item.content_html and "apple tree" in item.content_text


async def test_starred_feed_item_archive_fills_archive_text_only(session, user, fake_queue, bucket, browser):
    feed = models.Feed(user_id=user.id, url="https://news.example.com/feed.xml", title="Gazette")
    session.add(feed)
    await session.flush()
    item = models.Item(
        feed_id=feed.id,
        guid="g1",
        url=URL,
        title="Orchard survey (feed)",
        content_html="<p>Short teaser.</p>",
        content_text="Short teaser.",
        hash="h",
        published_at=datetime.now(UTC),
    )
    session.add(item)
    await session.commit()
    assert await save.archive_items(session, user, [item.id]) == 1
    snap = await session.scalar(select(models.Snapshot).where(models.Snapshot.item_id == item.id))
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(200, text=RENDERED_DOM, headers={"content-type": "text/html"})
        router.get(url__regex=r".*").respond(404)
        await capture.capture_snapshot(session, snap.id)
    await session.refresh(item)
    assert item.title == "Orchard survey (feed)" and item.content_text == "Short teaser."
    assert "apple tree" in item.archive_text and "/archive/a/" in item.reader_html
    assert item.full_text == item.archive_text  # the AI reads the archived page, not the teaser
    # Archived text is searchable too (weight C).
    found = await session.scalar(
        select(models.Item.id).where(
            models.Item.search_vector.op("@@")(func.websearch_to_tsquery("english", "saplings frost"))
        )
    )
    assert found == item.id
    # Not queued twice; and a user who turned it off gets nothing new.
    assert await save.archive_items(session, user, [item.id]) == 0
    user.settings = {"archive_starred": False}
    await session.commit()
    assert await save.archive_items(session, user, [item.id]) == 0


async def test_client_dom_from_the_extension_skips_the_browser(session, user, fake_queue, bucket, browser):
    """A signed-in or paywalled page: the DOM the user saw is the source; images are fetched server-side."""
    item, snap = await _saved(session, user, fake_queue)
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).respond(200, text="<html><body><p>Subscribe to read.</p></body></html>", headers={"content-type": "text/html"})
        router.get("https://news.example.com/pic.png").respond(200, content=PNG, headers={"content-type": "image/png"})
        router.get(url__regex=r".*").respond(404)
        done = await capture.capture_snapshot(session, snap.id, client_html=RENDERED_DOM)
    await session.refresh(item)
    assert done.status == "done" and done.render_mode == "client" and browser.calls == []
    assert "apple tree" in item.content_text and "/archive/a/" in item.content_html
