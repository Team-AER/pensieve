"""Capture one page: server HTML, rendered DOM, screenshot, frozen offline copy, article text, assets.

Order of work, each step optional so a partial capture still leaves something useful:
  1. raw pass: SSRF-guarded GET of the URL (the server's HTML, or the PDF / file itself)
  2. render pass: Chromium loads it, scripts run, lazy images load, full-page screenshot
  3. extraction: trafilatura over the rendered DOM *and* the raw HTML (or the DOM the extension sent)
  4. assets the browser didn't catch (the article's images, the site icon) are fetched with the same guard
  5. freeze: the DOM becomes a static page pointing at archive assets
  6. blobs to the bucket, rows to Postgres; a saved link's item gets the article text, so search and every
     AI step read the full page, and the AI pipeline is told about it like a new feed item
"""

from __future__ import annotations

import asyncio
import gzip
import html as html_lib
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.archive import extract
from pensieve.archive.freeze import ASSET_PREFIX, Freezer, collect_refs, pick_urls
from pensieve.archive.render import BROWSER_UA, Rendered, Renderer, RenderError, Resource, get_renderer
from pensieve.archive.storage import Storage, asset_key, get_storage, snapshot_prefix
from pensieve.config import get_settings
from pensieve.fetch import http as fetch_http
from pensieve.fetch.parse import content_hash
from pensieve.models import FEED_KIND_SAVED, ArchiveAsset, Feed, Item, Snapshot, SnapshotAsset

log = logging.getLogger(__name__)

PAGE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8"
MAX_EXTRA_FETCHES = 60
EXTRA_FETCH_BYTES = 8 * 1024 * 1024
RAW_HTML_LIMIT = 5 * 1024 * 1024
CLIENT_HTML_LIMIT = 8 * 1024 * 1024
ASSET_TYPES = ("image/", "font/", "text/css", "application/font", "application/x-font", "application/vnd.ms-fontobject")


@dataclass
class RawPage:
    url: str
    status: int
    content_type: str
    body: bytes

    @property
    def is_html(self) -> bool:
        ct = self.content_type
        return not ct or "html" in ct or ct.endswith("/xml") or ct == "application/xml"

    def text(self) -> str:
        head = self.body[:RAW_HTML_LIMIT]
        try:
            response = httpx.Response(200, content=head, headers={"content-type": self.content_type or "text/html"})
            return response.text
        except Exception:  # noqa: BLE001
            return head.decode("utf-8", errors="replace")


def _now() -> datetime:
    return datetime.now(UTC)


async def _raw(url: str) -> tuple[RawPage | None, str | None]:
    limit = get_settings().capture_max_file_mb * 1024 * 1024
    try:
        response = await fetch_http.get(url, headers={"User-Agent": BROWSER_UA, "Accept": PAGE_ACCEPT}, max_bytes=limit)
    except fetch_http.UnsafeURLError as exc:
        return None, f"refused: {exc}"
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, f"fetch failed: {exc.__class__.__name__}: {exc}"[:500]
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return RawPage(str(response.url), response.status_code, ctype, response.content), None


async def _fetch_extra(urls: list[str], resources: dict[str, Resource]) -> None:
    """Fetch assets the browser didn't load (or everything, with no browser); same SSRF guard as feeds."""
    sem = asyncio.Semaphore(6)
    budget = [get_settings().capture_max_asset_mb * 1024 * 1024 - sum(len(r.body) for r in resources.values())]

    async def one(url: str) -> None:
        async with sem:
            if budget[0] <= 0:
                return
            try:
                response = await fetch_http.get(url, headers={"User-Agent": BROWSER_UA}, max_bytes=EXTRA_FETCH_BYTES)
            except Exception:  # noqa: BLE001 - a missing image is not a failed capture
                return
            if response.status_code != 200:
                return
            ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not ctype.startswith(ASSET_TYPES) and not url.lower().endswith((".css", ".woff", ".woff2", ".ico")):
                return
            if ctype in {"", "application/octet-stream"} and url.lower().endswith(".css"):
                ctype = "text/css"
            budget[0] -= len(response.content)
            resources[url] = Resource(url, ctype or "application/octet-stream", response.content)

    await asyncio.gather(*(one(u) for u in urls))


def _pdf(data: bytes) -> tuple[str | None, str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    title = None
    try:
        title = (reader.metadata.title or "").strip() or None if reader.metadata else None
    except Exception:  # noqa: BLE001
        title = None
    pages = []
    for page in reader.pages[:400]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return title, "\n\n".join(pages)[: extract.TEXT_LIMIT], len(reader.pages)


def _paragraphs_html(text: str, limit: int = 200_000) -> str:
    blocks = [b.strip() for b in text[:limit].split("\n\n") if b.strip()]
    return "".join(f"<p>{html_lib.escape(' '.join(b.split()))}</p>" for b in blocks)


async def _store_assets(session: AsyncSession, storage: Storage | None, snapshot: Snapshot, used: dict[str, Resource]) -> int:
    await session.execute(delete(SnapshotAsset).where(SnapshotAsset.snapshot_id == snapshot.id))
    if not used or storage is None:
        return 0
    known = set(
        (await session.scalars(select(ArchiveAsset.sha256).where(ArchiveAsset.sha256.in_(list(used))))).all()
    )
    total = 0
    for sha, res in used.items():
        total += len(res.body)
        if sha in known:
            continue
        await storage.put(asset_key(sha), res.body, res.content_type)
        await session.execute(
            pg_insert(ArchiveAsset)
            .values(sha256=sha, content_type=res.content_type[:120], size=len(res.body))
            .on_conflict_do_nothing(index_elements=["sha256"])
        )
    await session.execute(
        pg_insert(SnapshotAsset)
        .values([{"snapshot_id": snapshot.id, "sha256": sha} for sha in used])
        .on_conflict_do_nothing()
    )
    return total


async def _drop_generation(storage: Storage | None, snapshot_id: uuid.UUID, generation: int) -> None:
    if storage is None or generation <= 0:
        return
    try:
        keys = [k async for k in storage.list_prefix(snapshot_prefix(snapshot_id, generation))]
        await storage.delete(keys)
    except Exception as exc:  # noqa: BLE001 - garbage collection sweeps anything left behind
        log.info("could not drop old capture blobs of %s: %s", snapshot_id, exc)


def _fallback_card(meta: dict, lead_local: str | None, url: str) -> str:
    parts = []
    if lead_local:
        parts.append(f'<p><img src="{lead_local}" alt=""></p>')
    if meta.get("description"):
        parts.append(f"<p>{html_lib.escape(meta['description'])}</p>")
    parts.append(
        f'<p><a href="{html_lib.escape(url)}">{html_lib.escape(url)}</a> did not have a readable article; '
        "the archived page and screenshot above show it as it looked.</p>"
    )
    return "".join(parts)


async def capture_snapshot(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
    *,
    client_html: str | None = None,
    renderer: Renderer | None = None,
    storage: Storage | None | bool = True,
) -> Snapshot | None:
    """Run one capture and commit its result. ``storage=True`` means the configured store (None = text only)."""
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None:
        return None
    item = await session.get(Item, snapshot.item_id)
    feed = await session.get(Feed, item.feed_id) if item else None
    if item is None or feed is None:
        return None
    store: Storage | None = get_storage() if storage is True else (storage or None)
    render = renderer if renderer is not None else get_renderer()
    old_generation = snapshot.generation
    snapshot.status = "rendering"
    snapshot.attempts += 1
    snapshot.generation += 1
    await session.commit()

    generation = snapshot.generation
    prefix = snapshot_prefix(snapshot.id, generation)
    url = snapshot.requested_url
    captured_at = _now()
    notes: list[str] = []
    bytes_total = 0

    raw, raw_error = await _raw(url)
    if raw_error:
        notes.append(raw_error)
    if raw is not None and raw.status >= 400:
        notes.append(f"server answered HTTP {raw.status}")

    # Non-HTML: keep the file itself, and its text when it has some.
    if raw is not None and raw.status < 400 and not raw.is_html and client_html is None:
        return await _capture_file(session, snapshot, item, feed, raw, store, prefix, captured_at, old_generation)

    rendered: Rendered | None = None
    if render is not None and client_html is None:
        try:
            rendered = await render.render(url)
        except (RenderError, TimeoutError) as exc:
            notes.append(f"render failed: {exc}"[:300])
        except Exception as exc:
            log.warning("render of %s crashed", url, exc_info=True)
            notes.append(f"render failed: {exc.__class__.__name__}")
    raw_html = raw.text() if raw is not None and raw.is_html and raw.status < 400 else None
    dom = (client_html[:CLIENT_HTML_LIMIT] if client_html else None) or (rendered.html if rendered else None)
    base = (rendered.url if rendered else None) or (raw.url if raw else None) or url
    if dom is None and raw_html is None:
        snapshot.status = "failed"
        snapshot.error = "; ".join(notes) or "nothing could be fetched"
        snapshot.final_url = base
        snapshot.http_status = raw.status if raw else (rendered.status if rendered else None)
        await session.commit()
        return snapshot

    ex = await asyncio.to_thread(extract.extract_best, dom, raw_html, base)
    meta = await asyncio.to_thread(extract.page_metadata, dom or raw_html, base)
    resources: dict[str, Resource] = dict(rendered.resources) if rendered else {}
    frozen_source = dom or raw_html or ""

    if store is not None:
        wanted = await asyncio.to_thread(collect_refs, frozen_source, base, resources)
        article_imgs = await asyncio.to_thread(collect_refs, ex.html, base, resources) if ex else []
        extra = [*article_imgs]
        for candidate in ((ex.image if ex else None) or meta.get("image"), rendered.icon_url if rendered else None):
            if candidate and candidate not in resources:
                extra.append(candidate)
        await _fetch_extra(pick_urls([*extra, *wanted], MAX_EXTRA_FETCHES), resources)

    # Without a bucket nothing can be served from /archive/a/, so nothing may point there.
    freezer = Freezer(resources if store is not None else {})
    page_html = await asyncio.to_thread(freezer.freeze, frozen_source, base, original_url=url, captured_at=captured_at)
    article_html = freezer.rewrite_article(ex.html, base) if ex else None
    lead_url = (ex.image if ex else None) or meta.get("image")
    lead_local = freezer.asset_url(lead_url)
    icon_local = freezer.asset_url(rendered.icon_url if rendered else None)

    if store is not None:
        if raw_html is not None:
            data = gzip.compress(raw_html.encode("utf-8", errors="replace"))
            await store.put(prefix + "raw.html.gz", data, "text/html; charset=utf-8", "gzip")
            snapshot.raw_key = prefix + "raw.html.gz"
            bytes_total += len(data)
        data = gzip.compress(page_html.encode("utf-8", errors="replace"))
        await store.put(prefix + "page.html.gz", data, "text/html; charset=utf-8", "gzip")
        snapshot.page_key = prefix + "page.html.gz"
        bytes_total += len(data)
        if rendered and rendered.screenshot:
            await store.put(prefix + "shot.jpg", rendered.screenshot, "image/jpeg")
            snapshot.shot_key = prefix + "shot.jpg"
            bytes_total += len(rendered.screenshot)
        else:
            snapshot.shot_key = None
        snapshot.file_key = None
        bytes_total += await _store_assets(session, store, snapshot, freezer.used)
    else:
        snapshot.raw_key = snapshot.page_key = snapshot.shot_key = snapshot.file_key = None

    title = (ex.title if ex else None) or meta.get("title") or (rendered.title if rendered else "") or ""
    text = ex.text if ex else extract.visible_text(frozen_source)
    snapshot.final_url = base[:2048]
    snapshot.http_status = (rendered.status if rendered else None) or (raw.status if raw else None)
    snapshot.content_type = (raw.content_type if raw else "text/html")[:120] or "text/html"
    snapshot.render_mode = "client" if client_html else ("browser" if rendered else "http")
    snapshot.site_name = ((ex.site_name if ex else None) or meta.get("site_name") or "")[:300] or None
    snapshot.byline = ((ex.byline if ex else None) or "")[:300] or None
    snapshot.lang = (ex.lang if ex else None) or meta.get("lang")
    snapshot.word_count = len(text.split())
    snapshot.page_published_at = (ex.published_at if ex else None) or meta.get("published_at")
    snapshot.lead_image_sha = lead_local[len(ASSET_PREFIX) :] if lead_local else None
    snapshot.icon_sha = icon_local[len(ASSET_PREFIX) :] if icon_local else None
    snapshot.bytes_total = bytes_total
    snapshot.captured_at = captured_at
    snapshot.status = "done"
    snapshot.error = "; ".join(notes)[:2000] or None

    body_html = article_html or _fallback_card(meta, lead_local, url)
    if feed.kind == FEED_KIND_SAVED:
        if title:
            item.title = title[:1000]
        item.author = snapshot.byline
        item.content_html = body_html
        item.content_text = text
        item.reader_html = article_html
        item.reader_fetched_at = captured_at
        item.hash = content_hash(item.title, text)
        item.updated_at = captured_at
    else:
        item.archive_text = text or None
        if article_html and not item.reader_html:
            item.reader_html = article_html
            item.reader_fetched_at = captured_at
    await session.commit()
    await _drop_generation(store, snapshot.id, old_generation)
    if feed.kind == FEED_KIND_SAVED:
        await _notify_ai(feed, item)
    return snapshot


async def _capture_file(
    session: AsyncSession,
    snapshot: Snapshot,
    item: Item,
    feed: Feed,
    raw: RawPage,
    store: Storage | None,
    prefix: str,
    captured_at: datetime,
    old_generation: int,
) -> Snapshot:
    title, text, pages = None, "", 0
    if raw.content_type == "application/pdf" or raw.body[:5] == b"%PDF-":
        try:
            title, text, pages = await asyncio.to_thread(_pdf, raw.body)
        except Exception as exc:  # noqa: BLE001
            snapshot.error = f"PDF text could not be read: {exc.__class__.__name__}"
    elif raw.content_type.startswith("text/"):
        text = raw.body[: extract.TEXT_LIMIT].decode("utf-8", errors="replace")
    snapshot.file_key = snapshot.page_key = snapshot.shot_key = snapshot.raw_key = None
    if store is not None:
        await store.put(prefix + "file", raw.body, raw.content_type or "application/octet-stream")
        snapshot.file_key = prefix + "file"
        snapshot.bytes_total = len(raw.body)
        await session.execute(delete(SnapshotAsset).where(SnapshotAsset.snapshot_id == snapshot.id))
    file_url = f"/archive/s/{snapshot.id}/file"
    if raw.content_type.startswith("image/") and store is not None:
        body_html = f'<p><img src="{file_url}" alt=""></p>'
    elif text:
        body_html = _paragraphs_html(text)
    else:
        body_html = f"<p>A {html_lib.escape(raw.content_type or 'file')} saved as-is.</p>"
    snapshot.final_url = raw.url[:2048]
    snapshot.http_status = raw.status
    snapshot.content_type = (raw.content_type or "application/octet-stream")[:120]
    snapshot.render_mode = "file"
    snapshot.word_count = len(text.split())
    snapshot.captured_at = captured_at
    snapshot.status = "done"
    if feed.kind == FEED_KIND_SAVED:
        name = title or raw.url.rstrip("/").rsplit("/", 1)[-1] or raw.url
        item.title = (name + (f" ({pages} pages)" if pages and not title else ""))[:1000]
        item.content_html = body_html
        item.content_text = text
        item.hash = content_hash(item.title, text)
        item.updated_at = captured_at
    else:
        item.archive_text = text or None
    await session.commit()
    await _drop_generation(store, snapshot.id, old_generation)
    if feed.kind == FEED_KIND_SAVED:
        await _notify_ai(feed, item)
    return snapshot


async def _notify_ai(feed: Feed, item: Item) -> None:
    from pensieve.fetch.subscribe import _notify_new_items

    await _notify_new_items(feed, [item])


__all__ = ["capture_snapshot"]
