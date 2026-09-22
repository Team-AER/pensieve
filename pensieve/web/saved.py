"""Saved links: the Save dialog, the bookmarklet / share-sheet page, the token API, archive views and blobs,
and Manage > Saving.

Archived pages are untrusted HTML. They are served with a CSP that runs no script and loads nothing but
Pensieve's own archive assets, inside an iframe sandbox without allow-scripts, and never on a path a
script could use: ``/archive/s/<snapshot>/page``.
"""

from __future__ import annotations

import gzip
import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.auth import generate_api_token, hash_api_token, user_from_api_token
from pensieve.config import get_settings
from pensieve.models import (
    FEED_KIND_SAVED,
    ApiToken,
    ArchiveAsset,
    Feed,
    Item,
    Snapshot,
    SnapshotAsset,
    User,
)
from pensieve.web.templating import DB, CsrfUser, CurrentUser, hx_trigger, render

log = logging.getLogger(__name__)
router = APIRouter()

#: CSP for an archived page: no script, no network except Pensieve's archive assets, no forms, no plugins.
#: ``sandbox`` without allow-scripts also covers a direct top-level visit; allow-same-origin lets the
#: sandboxed page load /archive/a/ assets with the session cookie (it cannot run code to abuse that).
ARCHIVE_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self' data:; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'self'; "
    "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox"
)
#: For single blobs (images, fonts, screenshots): never executable, even an SVG opened on its own.
BLOB_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox"
VIEW_MODES = ("article", "page", "shot", "file")


def parse_tags(raw: str | list[str] | None) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = (raw or "").replace(";", ",").split(",")
    return [v.strip().lstrip("#") for v in values if v and v.strip()][:20]


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


@router.post("/saved")
async def save_from_dialog(
    request: Request,
    user: CsrfUser,
    session: DB,
    url: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    from pensieve.archive.save import SaveError, save_link

    try:
        result = await save_link(session, user, url, tags=parse_tags(tags), note=note)
    except SaveError as exc:
        return render(request, "partials/save_result.html", {"error": str(exc)}, user=user)
    headers = hx_trigger(
        "counts-changed", **{"link-saved": {"id": str(result.item.id), "created": result.created}}
    )
    return render(request, "partials/save_result.html", {"result": result, "error": None}, user=user, headers=headers)


@router.get("/save")
async def save_page(
    request: Request,
    user: CurrentUser,
    url: str = "",
    title: str = "",
    text: str = "",
):
    """The bookmarklet popup and the PWA share target: a one-tap confirmation, never a save on GET."""
    from pensieve.archive.save import find_url

    target = find_url(url, text, title) or ""
    if title and (title.strip() == target or title.strip().startswith(("http://", "https://"))):
        title = ""
    error = None if target or not (url or text or title) else "Nothing shared looked like a link."
    ctx = {"url": target, "title": title.strip()[:300], "error": error, "done": None, "popup": True}
    return render(request, "save.html", ctx, user=user)


@router.post("/save")
async def save_submit(
    request: Request,
    user: CsrfUser,
    session: DB,
    url: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    from pensieve.archive.save import SaveError, save_link

    try:
        result = await save_link(session, user, url, title=title or None, tags=parse_tags(tags), note=note)
    except SaveError as exc:
        ctx = {"url": url, "title": title, "error": str(exc), "done": None, "popup": True}
        return render(request, "save.html", ctx, user=user, status_code=422)
    ctx = {"url": url, "title": title, "error": None, "done": result, "popup": True}
    return render(request, "save.html", ctx, user=user)


async def api_user(request: Request, session: AsyncSession) -> User:
    """``Authorization: Bearer <token>`` with any API token from Manage > API tokens (or Saving)."""
    header = request.headers.get("authorization", "")
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    elif header.lower().startswith("googlelogin auth="):
        token = header.split("=", 1)[1].strip()
    user = await user_from_api_token(token, session) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="A valid API token is required (Authorization: Bearer <token>)")
    return user


@router.post("/api/v1/save")
async def api_save(request: Request, session: DB):
    """Save a link from a script, the iOS Shortcut or the browser extension.

    Body: JSON or form with ``url`` (required), ``title``, ``tags`` (list or comma string), ``note`` and,
    from the extension, ``html``: the DOM the user is looking at (logged-in or paywalled pages).
    """
    user = await api_user(request, session)
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
    else:
        body = dict(await request.form())
    from pensieve.archive.save import SaveError, find_url, save_link

    url = find_url(str(body.get("url") or ""), str(body.get("text") or "")) or str(body.get("url") or "")
    html = body.get("html")
    try:
        result = await save_link(
            session,
            user,
            url,
            title=str(body.get("title") or "") or None,
            tags=parse_tags(body.get("tags")),
            note=str(body.get("note") or "") or None,
            client_html=html if isinstance(html, str) and html.strip() else None,
        )
    except SaveError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    base = get_settings().base_url.rstrip("/")
    return JSONResponse(
        {
            "id": str(result.item.id),
            "url": result.item.url,
            "title": result.item.title,
            "created": result.created,
            "status": result.snapshot.status,
            "open": f"{base}/items/{result.item.id}",
        },
        status_code=201 if result.created else 200,
    )


# ---------------------------------------------------------------------------
# Archive views in the article pane
# ---------------------------------------------------------------------------


async def _owned_item(session: AsyncSession, user: User, item_id: uuid.UUID) -> tuple[Item, Feed]:
    from pensieve.web.queries import get_user_item

    pair = await get_user_item(session, user, item_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return pair


async def _body(request: Request, session: AsyncSession, user: User, item: Item, feed: Feed, mode: str, **extra):
    from pensieve.web.items import article_context

    ctx = await article_context(session, user, item, feed)
    ctx["archive_mode"] = mode if mode in VIEW_MODES else "article"
    ctx["auto_reader"] = False
    ctx.update(extra)
    return render(request, "partials/body.html", ctx, user=user)


@router.get("/items/{item_id}/view/{mode}")
async def archive_view(request: Request, item_id: uuid.UUID, mode: str, user: CurrentUser, session: DB):
    item, feed = await _owned_item(session, user, item_id)
    return await _body(request, session, user, item, feed, mode)


@router.get("/items/{item_id}/capture")
async def capture_poll(request: Request, item_id: uuid.UUID, user: CurrentUser, session: DB):
    """Polled while a capture runs: 204 (keep polling) until it settles, then the refreshed article."""
    item, feed = await _owned_item(session, user, item_id)
    snap = await session.scalar(select(Snapshot).where(Snapshot.user_id == user.id, Snapshot.item_id == item.id))
    if snap is not None and snap.status in {"queued", "rendering"}:
        return Response(status_code=204)
    from pensieve.web.items import article_context

    ctx = await article_context(session, user, item, feed)
    ctx["auto_open"] = False
    headers = {"HX-Retarget": "#article", "HX-Reswap": "innerHTML"} | hx_trigger("capture-done")
    return render(request, "partials/article.html", ctx, user=user, headers=headers)


@router.post("/items/{item_id}/recapture")
async def recapture(request: Request, item_id: uuid.UUID, user: CsrfUser, session: DB):
    """Capture the page again (Save a copy on a feed item, or a fresh copy of a saved link)."""
    from pensieve.archive.save import SaveError, enqueue_capture, ensure_snapshot, normalize_url

    item, feed = await _owned_item(session, user, item_id)
    if not item.url:
        return await _body(request, session, user, item, feed, "article", error="This item has no link to archive.")
    try:
        url = normalize_url(item.url)
    except SaveError as exc:
        return await _body(request, session, user, item, feed, "article", error=str(exc))
    snap, _ = await ensure_snapshot(session, user.id, item, url, force=True)
    await session.commit()
    await enqueue_capture(snap.id)
    return await _body(request, session, user, item, feed, "article")


@router.post("/items/{item_id}/save-link")
async def save_item_link(request: Request, item_id: uuid.UUID, user: CsrfUser, session: DB):
    """Save a feed item's link to My list (read it later, keep a copy)."""
    from pensieve.archive.save import SaveError, save_link

    item, _feed = await _owned_item(session, user, item_id)
    if not item.url:
        raise HTTPException(status_code=400, detail="This item has no link")
    try:
        result = await save_link(session, user, item.url, title=item.title)
    except SaveError as exc:
        return Response(status_code=204, headers=hx_trigger(toast={"text": str(exc)}))
    return Response(
        status_code=204,
        headers=hx_trigger("counts-changed", **{"link-saved": {"id": str(result.item.id), "created": result.created}}),
    )


# ---------------------------------------------------------------------------
# Archive blobs
# ---------------------------------------------------------------------------


async def _owned_snapshot(session: AsyncSession, user: User, snapshot_id: uuid.UUID) -> Snapshot:
    snap = await session.get(Snapshot, snapshot_id)
    if snap is None or snap.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return snap


async def _blob(key: str | None) -> bytes:
    from pensieve.archive.storage import get_storage

    storage = get_storage()
    if not key or storage is None:
        raise HTTPException(status_code=404, detail="Not archived")
    data = await storage.get(key)
    if data is None:
        raise HTTPException(status_code=404, detail="Not archived")
    return data


def _gzip_html(request: Request, data: bytes, headers: dict[str, str]) -> Response:
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(data, media_type="text/html; charset=utf-8", headers=headers | {"Content-Encoding": "gzip"})
    return Response(gzip.decompress(data), media_type="text/html; charset=utf-8", headers=headers)


PRIVATE_CACHE = {"Cache-Control": "private, max-age=86400", "Vary": "Cookie, Accept-Encoding"}


@router.get("/archive/s/{snapshot_id}/page")
async def archived_page(request: Request, snapshot_id: uuid.UUID, user: CurrentUser, session: DB):
    snap = await _owned_snapshot(session, user, snapshot_id)
    data = await _blob(snap.page_key)
    headers = {"Content-Security-Policy": ARCHIVE_CSP, "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "no-referrer"}
    return _gzip_html(request, data, headers | PRIVATE_CACHE)


@router.get("/archive/s/{snapshot_id}/source")
async def archived_source(request: Request, snapshot_id: uuid.UUID, user: CurrentUser, session: DB):
    """The server HTML as fetched, shown as text (never rendered)."""
    snap = await _owned_snapshot(session, user, snapshot_id)
    data = gzip.decompress(await _blob(snap.raw_key))
    return Response(data, media_type="text/plain; charset=utf-8", headers={"Content-Security-Policy": BLOB_CSP} | PRIVATE_CACHE)


@router.get("/archive/s/{snapshot_id}/shot")
async def archived_shot(snapshot_id: uuid.UUID, user: CurrentUser, session: DB):
    snap = await _owned_snapshot(session, user, snapshot_id)
    data = await _blob(snap.shot_key)
    return Response(data, media_type="image/jpeg", headers={"Content-Security-Policy": BLOB_CSP} | PRIVATE_CACHE)


@router.get("/archive/s/{snapshot_id}/file")
async def archived_file(snapshot_id: uuid.UUID, user: CurrentUser, session: DB):
    snap = await _owned_snapshot(session, user, snapshot_id)
    data = await _blob(snap.file_key)
    ctype = snap.content_type or "application/octet-stream"
    headers = dict(PRIVATE_CACHE) | {"Content-Disposition": "inline"}
    if ctype != "application/pdf":
        headers["Content-Security-Policy"] = BLOB_CSP  # the browser's PDF viewer needs the default policy
    return Response(data, media_type=ctype, headers=headers)


@router.get("/archive/a/{sha256}")
async def archived_asset(sha256: str, user: CurrentUser, session: DB):
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise HTTPException(status_code=404)
    owned = await session.scalar(
        select(ArchiveAsset.content_type)
        .join(SnapshotAsset, SnapshotAsset.sha256 == ArchiveAsset.sha256)
        .join(Snapshot, Snapshot.id == SnapshotAsset.snapshot_id)
        .where(ArchiveAsset.sha256 == sha256, Snapshot.user_id == user.id)
        .limit(1)
    )
    if owned is None:
        raise HTTPException(status_code=404)
    from pensieve.archive.storage import asset_key

    data = await _blob(asset_key(sha256))
    headers = {"Content-Security-Policy": BLOB_CSP, "Cache-Control": "private, max-age=31536000, immutable"}
    return Response(data, media_type=owned, headers=headers)


# ---------------------------------------------------------------------------
# Manage > Saving
# ---------------------------------------------------------------------------


SAVING_FLASH = {
    "settings_saved": "Settings saved.",
    "import_started": "Import started. Links appear in Saved right away; their pages are captured in the background.",
    "retry_queued": "Failed captures queued again.",
    "backfill_queued": "Starred items queued for archiving.",
}


async def saving_stats(session: AsyncSession, user: User) -> dict:
    saved = await session.scalar(
        select(func.count(Item.id)).join(Feed, Feed.id == Item.feed_id).where(
            Feed.user_id == user.id, Feed.kind == FEED_KIND_SAVED
        )
    )
    by_status = dict(
        (
            await session.execute(
                select(Snapshot.status, func.count()).where(Snapshot.user_id == user.id).group_by(Snapshot.status)
            )
        ).all()
    )
    snap_bytes = await session.scalar(select(func.coalesce(func.sum(Snapshot.bytes_total), 0)).where(Snapshot.user_id == user.id))
    return {
        "saved": int(saved or 0),
        "done": int(by_status.get("done", 0)),
        "pending": int(by_status.get("queued", 0)) + int(by_status.get("rendering", 0)),
        "failed": int(by_status.get("failed", 0)),
        "bytes": int(snap_bytes or 0),
    }


def bookmarklet(base_url: str) -> str:
    base = base_url.rstrip("/")
    return (
        "javascript:(()=>{window.open('" + base + "/save?url='+encodeURIComponent(location.href)"
        "+'&title='+encodeURIComponent(document.title),'pensieve-save','width=460,height=600')})()"
    )


async def _saving_page(request: Request, session: AsyncSession, user: User, new_token: str | None = None, error: str | None = None):
    from pensieve.archive.storage import get_storage
    from pensieve.web.manage import page

    settings = get_settings()
    base = str(request.base_url).rstrip("/") if settings.debug else settings.base_url.rstrip("/")
    failed = list(
        await session.scalars(
            select(Snapshot).where(Snapshot.user_id == user.id, Snapshot.status == "failed").order_by(Snapshot.updated_at.desc()).limit(8)
        )
    )
    ctx = {
        "stats": await saving_stats(session, user),
        "storage_on": get_storage() is not None,
        "browser_on": bool(settings.browser_ws),
        "bookmarklet": bookmarklet(base),
        "base": base,
        "archive_starred": bool((user.settings or {}).get("archive_starred", True)),
        "new_token": new_token,
        "failed": failed,
        "error": error,
    }
    msg = request.query_params.get("msg")
    if msg in SAVING_FLASH:
        ctx["saving_flash"] = SAVING_FLASH[msg]
    return page(request, user, "saving", "manage/saving.html", ctx)


@router.get("/manage/saving")
async def saving_page(request: Request, user: CurrentUser, session: DB):
    return await _saving_page(request, session, user)


@router.post("/manage/saving/settings")
async def saving_settings(
    request: Request, user: CsrfUser, session: DB, archive_starred: Annotated[str, Form()] = ""
):
    db_user = await session.get(User, user.id)
    settings = dict(db_user.settings or {})
    settings["archive_starred"] = archive_starred in {"1", "on", "true"}
    db_user.settings = settings
    await session.commit()
    return RedirectResponse("/manage/saving?msg=settings_saved", status_code=303)


@router.post("/manage/saving/token")
async def saving_token(request: Request, user: CsrfUser, session: DB, label: Annotated[str, Form()] = ""):
    plaintext = generate_api_token()
    session.add(
        ApiToken(user_id=user.id, label=(label.strip() or "Save from my phone")[:120], kind="web", token_hash=hash_api_token(plaintext))
    )
    await session.commit()
    return await _saving_page(request, session, user, new_token=plaintext)


@router.post("/manage/saving/import")
async def saving_import(request: Request, user: CsrfUser, session: DB, file: Annotated[UploadFile, File()]):
    from pensieve.archive.imports import parse_export

    data = await file.read(40 * 1024 * 1024 + 1)
    if len(data) > 40 * 1024 * 1024:
        return await _saving_page(request, session, user, error="That file is larger than 40 MB.")
    try:
        links = parse_export(file.filename or "", data)
    except Exception as exc:  # noqa: BLE001
        log.info("import parse failed: %s", exc)
        links = []
    if not links:
        return await _saving_page(
            request, session, user,
            error="No links found. Use Pocket's export (HTML or CSV), Instapaper's CSV, or a browser bookmarks file.",
        )
    try:
        await queue.enqueue(queue.CAPTURE_IMPORT, str(user.id), [link.as_job() for link in links])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enqueue import: %s", exc)
        return await _saving_page(request, session, user, error="The background queue is unavailable right now.")
    return RedirectResponse("/manage/saving?msg=import_started", status_code=303)


@router.post("/manage/saving/retry")
async def saving_retry(request: Request, user: CsrfUser, session: DB):
    from pensieve.archive.save import requeue_failed

    await requeue_failed(session, user.id)
    return RedirectResponse("/manage/saving?msg=retry_queued", status_code=303)


@router.post("/manage/saving/backfill")
async def saving_backfill(request: Request, user: CsrfUser, session: DB):
    from pensieve.archive.save import archive_starred_backlog

    await archive_starred_backlog(session, user.id)
    return RedirectResponse("/manage/saving?msg=backfill_queued", status_code=303)
