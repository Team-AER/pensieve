"""Settings and management pages under /manage."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.auth import (
    generate_api_token,
    hash_api_token,
    hash_password,
    verify_password,
)
from pensieve.config import get_settings
from pensieve.models import (
    AIJob,
    ApiToken,
    Feed,
    FeedRule,
    Folder,
    Item,
    ItemState,
    Note,
    Profile,
    Tag,
    User,
    UserRole,
)
from pensieve.web.queries import invalidate_nav_cache, parse_uuid
from pensieve.web.templating import (
    DB,
    READING_PREFS,
    THEMES,
    CsrfUser,
    CurrentUser,
    hx_trigger,
    is_htmx,
    render,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/manage")

FLASH = {
    "feed_added": "Feed added.",
    "feed_updated": "Feed updated.",
    "feed_removed": "Unsubscribed.",
    "folder_saved": "Folder saved.",
    "folder_removed": "Folder removed.",
    "tag_saved": "Tag saved.",
    "tag_removed": "Tag removed.",
    "rule_saved": "Rule saved.",
    "rule_removed": "Rule removed.",
    "settings_saved": "Settings saved.",
    "profile_saved": "Profile saved as a new version.",
    "account_saved": "Account updated.",
    "password_changed": "Password changed.",
    "token_revoked": "Token revoked.",
    "user_saved": "User updated.",
    "user_removed": "User removed.",
    "opml_imported": "OPML imported.",
    "feeds_resumed": "All paused feeds resumed.",
    "rule_applied": "Rule applied to existing items.",
    "suggestion_dismissed": "Suggestion dismissed.",
}

AI_TOGGLES = ("auto_file", "tag_items", "group_stories", "memory", "digest")

SECTIONS = [
    ("feeds", "Feeds", "/manage/feeds"),
    ("folders", "Folders and tags", "/manage/folders"),
    ("rules", "Rules and filters", "/manage/rules"),
    ("ai", "AI and memory", "/manage/ai"),
    ("import", "Import and export", "/manage/import"),
]
ACCOUNT_SECTIONS = [
    ("account", "Profile and password", "/manage/account"),
    ("tokens", "API tokens", "/manage/tokens"),
    ("users", "Household users", "/manage/users"),
]


def page(request: Request, user: User, section: str, template: str, ctx: dict[str, Any], status_code: int = 200):
    msg = request.query_params.get("msg")
    base = {
        "section": section,
        "sections": SECTIONS,
        "account_sections": ACCOUNT_SECTIONS,
        "flash": FLASH.get(msg or "", None),
        "error": ctx.pop("error", None),
    }
    base.update(ctx)
    return render(request, template, base, user=user, status_code=status_code)


def back(path: str, msg: str | None = None) -> RedirectResponse:
    url = f"{path}?msg={msg}" if msg else path
    return RedirectResponse(url, status_code=303)


async def get_folder(session: AsyncSession, user: User, folder_id: uuid.UUID | None) -> Folder | None:
    if folder_id is None:
        return None
    folder = await session.get(Folder, folder_id)
    return folder if folder and folder.user_id == user.id else None


async def get_feed_or_404(session: AsyncSession, user: User, feed_id: uuid.UUID) -> Feed:
    feed = await session.get(Feed, feed_id)
    if feed is None or feed.user_id != user.id:
        raise HTTPException(status_code=404, detail="Feed not found")
    return feed


async def user_folders(session: AsyncSession, user: User) -> list[Folder]:
    return list(
        await session.scalars(select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name))
    )


@router.get("")
@router.get("/")
async def manage_root():
    return RedirectResponse("/manage/feeds", status_code=303)


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


def feed_health(feed: Feed) -> tuple[str, str]:
    if feed.paused:
        return "dot", "Paused"
    if feed.error_count >= 5:
        return "dot-bad", feed.last_error or "Failing"
    if feed.error_count > 0:
        return "dot-warn", feed.last_error or "Recent errors"
    if feed.last_success_at is None:
        return "dot", "Not fetched yet"
    return "dot-ok", "Healthy"


@router.get("/feeds")
async def feeds_page(
    request: Request,
    user: CurrentUser,
    session: DB,
    q: str = "",
    folder: str = "",
):
    return await _feeds(request, user, session, q, folder, error=request.query_params.get("err"))


async def _feeds(
    request: Request,
    user: User,
    session: AsyncSession,
    q: str = "",
    folder: str = "",
    error: str | None = None,
    status_code: int = 200,
):
    stmt = select(Feed).where(Feed.user_id == user.id).order_by(Feed.title, Feed.url)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(Feed.title).like(like) | func.lower(Feed.url).like(like))
    if folder == "inbox":
        stmt = stmt.where(Feed.folder_id.is_(None))
    elif fid := parse_uuid(folder):
        stmt = stmt.where(Feed.folder_id == fid)
    feeds = list(await session.scalars(stmt))
    folders = await user_folders(session, user)
    folder_names = {f.id: f.name for f in folders}
    stats = await session.execute(
        select(
            Item.feed_id,
            func.count(Item.id),
            func.count(ItemState.item_id).filter(ItemState.is_read.is_(True)),
        )
        .select_from(Item)
        .join(Feed, Feed.id == Item.feed_id)
        .outerjoin(ItemState, (ItemState.item_id == Item.id) & (ItemState.user_id == user.id))
        .where(Feed.user_id == user.id)
        .group_by(Item.feed_id)
    )
    open_rates = {fid: (int(read) / int(total) if total else None) for fid, total, read in stats}
    rows = []
    for f in feeds:
        dot, health = feed_health(f)
        rows.append(
            {
                "feed": f,
                "folder_name": folder_names.get(f.folder_id),
                "suggested_name": folder_names.get(f.suggested_folder_id) if f.folder_id is None else None,
                "dot": dot,
                "health": health,
                "open_rate": open_rates.get(f.id),
            }
        )
    paused_count = int(
        await session.scalar(select(func.count(Feed.id)).where(Feed.user_id == user.id, Feed.paused.is_(True))) or 0
    )
    return page(
        request,
        user,
        "feeds",
        "manage/feeds.html",
        {
            "rows": rows,
            "folders": folders,
            "q": q,
            "folder": folder,
            "error": error,
            "paused_count": paused_count,
            "interval_bounds": (get_settings().fetch_min_interval_min, get_settings().fetch_max_interval_min),
        },
        status_code=status_code,
    )


@router.post("/feeds")
async def add_feed_route(
    request: Request,
    user: CsrfUser,
    session: DB,
    url: Annotated[str, Form()] = "",
    folder_id: Annotated[str, Form()] = "",
):
    url = url.strip()
    folder = await get_folder(session, user, parse_uuid(folder_id))
    if not url:
        return await _feeds(request, user, session, error="Enter a feed or site URL.", status_code=400)
    try:
        from pensieve.fetch.subscribe import add_feed  # type: ignore[import-not-found]
    except ImportError:
        return await _feeds(request, user, session, error="Subscribing isn't available yet.", status_code=400)
    try:
        await add_feed(session, user, url, folder.id if folder else None)
        await session.commit()
    except Exception as exc:  # noqa: BLE001  FeedError (and anything else) surfaces as a message
        await session.rollback()
        await session.refresh(user)
        return await _feeds(request, user, session, error=str(exc) or "Couldn't add that feed.", status_code=400)
    return back("/manage/feeds", "feed_added")


@router.post("/feeds/{feed_id}/rename")
async def rename_feed(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    title: Annotated[str, Form()] = "",
):
    feed = await get_feed_or_404(session, user, feed_id)
    if title.strip():
        feed.title = title.strip()[:300]
        await session.commit()
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/{feed_id}/move")
async def move_feed(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    folder_id: Annotated[str, Form()] = "",
):
    feed = await get_feed_or_404(session, user, feed_id)
    folder = await get_folder(session, user, parse_uuid(folder_id))
    feed.folder_id = folder.id if folder else None
    await session.commit()
    invalidate_nav_cache(user.id)
    if is_htmx(request):
        # Drag-and-drop in the reader nav: no page to render, just refresh the counts/tree.
        return Response(status_code=204, headers=hx_trigger("counts-changed"))
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/resume-all")
async def resume_all_feeds(
    request: Request,
    user: CsrfUser,
    session: DB,
):
    now = datetime.now(UTC)
    await session.execute(
        Feed.__table__.update()
        .where(Feed.user_id == user.id, Feed.paused.is_(True))
        .values(paused=False, error_count=0, next_fetch_at=now)
    )
    await session.commit()
    return back("/manage/feeds", "feeds_resumed")


@router.post("/feeds/{feed_id}/interval")
async def set_feed_interval(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    fetch_interval_min: Annotated[str, Form()] = "",
):
    feed = await get_feed_or_404(session, user, feed_id)
    settings = get_settings()
    try:
        minutes = int(fetch_interval_min)
    except ValueError:
        return RedirectResponse("/manage/feeds?err=Enter+the+poll+interval+in+minutes.", status_code=303)
    minutes = max(settings.fetch_min_interval_min, min(settings.fetch_max_interval_min, minutes))
    feed.fetch_interval_min = minutes
    base = feed.last_fetch_at or datetime.now(UTC)
    feed.next_fetch_at = min(base + timedelta(minutes=minutes), datetime.now(UTC) + timedelta(minutes=minutes))
    await session.commit()
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/{feed_id}/refresh")
async def refresh_feed_now(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    """Queue an immediate poll of one feed (context menu "Refresh now"); the fetch worker does the rest."""
    feed = await get_feed_or_404(session, user, feed_id)
    from pensieve import queue

    try:
        await queue.enqueue(queue.FETCH_FEED, str(feed.id), _job_id=queue.job_id_for("fetch", feed.id))
    except Exception as exc:
        log.warning("could not enqueue refresh for feed %s: %s", feed.id, exc)
        raise HTTPException(status_code=503, detail="The fetch queue is unavailable") from exc
    if is_htmx(request):
        return Response(status_code=204)
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/{feed_id}/pause")
async def pause_feed(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    feed = await get_feed_or_404(session, user, feed_id)
    feed.paused = True
    await session.commit()
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/{feed_id}/resume")
async def resume_feed(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    feed = await get_feed_or_404(session, user, feed_id)
    feed.paused = False
    feed.error_count = 0
    feed.next_fetch_at = datetime.now(UTC)
    await session.commit()
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/{feed_id}/unsubscribe")
async def unsubscribe_feed(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    feed = await get_feed_or_404(session, user, feed_id)
    await session.delete(feed)
    await session.commit()
    return back("/manage/feeds", "feed_removed")


@router.post("/feeds/{feed_id}/accept-suggestion")
async def accept_suggestion(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    folder_id: Annotated[str, Form()] = "",
):
    feed = await get_feed_or_404(session, user, feed_id)
    chosen = await get_folder(session, user, parse_uuid(folder_id)) if folder_id else None
    suggested = await get_folder(session, user, feed.suggested_folder_id)
    target = chosen or suggested
    if target is None:
        return back("/manage/feeds")
    feed.folder_id = target.id
    if suggested is not None and chosen is not None and chosen.id != suggested.id:
        try:
            from pensieve.ai.service import record_correction  # type: ignore[import-not-found]

            await record_correction(
                session, user, "feed_folder", feed.id, "folder_id", str(suggested.id), str(chosen.id)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("record_correction unavailable: %s", exc)
    await session.commit()
    return back("/manage/feeds", "feed_updated")


@router.post("/feeds/{feed_id}/dismiss-suggestion")
async def dismiss_suggestion(
    request: Request,
    feed_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    """Reject the AI's folder suggestion for a feed (records the correction via the ai package when present)."""
    feed = await get_feed_or_404(session, user, feed_id)
    try:
        from pensieve.ai.service import dismiss_folder_suggestion  # type: ignore[import-not-found]
    except (ImportError, AttributeError):
        feed.suggested_folder_id = None
        feed.suggested_folder_name = None
        feed.suggested_folder_confidence = None
        await session.commit()
        return Response(status_code=204) if is_htmx(request) else back("/manage/feeds")
    try:
        await dismiss_folder_suggestion(session, user, feed)
        await session.commit()
    except Exception as exc:  # noqa: BLE001  AI is additive; never let it break the page
        log.warning("dismiss_folder_suggestion failed for %s: %s", feed.id, exc)
        await session.rollback()
        feed = await get_feed_or_404(session, user, feed_id)
        feed.suggested_folder_id = None
        feed.suggested_folder_name = None
        feed.suggested_folder_confidence = None
        await session.commit()
    if is_htmx(request):
        return Response(status_code=204)
    return back("/manage/feeds", "suggestion_dismissed")


# ---------------------------------------------------------------------------
# Folders and tags
# ---------------------------------------------------------------------------


@router.get("/folders")
async def folders_page(
    request: Request, user: CurrentUser, session: DB,
):
    folders = await user_folders(session, user)
    counts = dict(
        (await session.execute(
            select(Feed.folder_id, func.count(Feed.id)).where(Feed.user_id == user.id).group_by(Feed.folder_id)
        )).all()
    )
    tags = list(await session.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.kind, Tag.position, Tag.name)))
    return page(
        request,
        user,
        "folders",
        "manage/folders.html",
        {"folders": folders, "feed_counts": counts, "tags": tags, "error": request.query_params.get("err")},
    )


@router.post("/folders")
async def create_folder(
    request: Request,
    user: CsrfUser,
    session: DB,
    name: Annotated[str, Form()] = "",
):
    name = name.strip()[:120]
    if not name:
        return back("/manage/folders")
    exists_ = await session.scalar(select(Folder).where(Folder.user_id == user.id, Folder.name == name))
    if exists_:
        return RedirectResponse("/manage/folders?err=A+folder+with+that+name+already+exists.", status_code=303)
    position = int(await session.scalar(select(func.coalesce(func.max(Folder.position), 0)).where(Folder.user_id == user.id)) or 0)
    session.add(Folder(user_id=user.id, name=name, position=position + 1))
    await session.commit()
    return back("/manage/folders", "folder_saved")


@router.post("/folders/reorder")
async def reorder_folders(
    request: Request,
    user: CsrfUser,
    session: DB,
    order: Annotated[str, Form()] = "",
):
    ids = [u for u in (parse_uuid(p) for p in order.split(",")) if u]
    folders = {f.id: f for f in await user_folders(session, user)}
    for pos, fid in enumerate(ids):
        if fid in folders:
            folders[fid].position = pos
    await session.commit()
    return back("/manage/folders", "folder_saved")


@router.post("/folders/{folder_id}/rename")
async def rename_folder(
    request: Request,
    folder_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    name: Annotated[str, Form()] = "",
):
    folder = await get_folder(session, user, folder_id)
    if folder is None:
        raise HTTPException(status_code=404)
    if name.strip():
        folder.name = name.strip()[:120]
        await session.commit()
    return back("/manage/folders", "folder_saved")


@router.post("/folders/{folder_id}/delete")
async def delete_folder(
    request: Request,
    folder_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    folder = await get_folder(session, user, folder_id)
    if folder is None:
        raise HTTPException(status_code=404)
    await session.delete(folder)  # feeds fall back to Inbox (ON DELETE SET NULL)
    await session.commit()
    return back("/manage/folders", "folder_removed")


@router.post("/tags")
async def create_tag(
    request: Request,
    user: CsrfUser,
    session: DB,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "user",
    description: Annotated[str, Form()] = "",
):
    name = name.strip()[:120]
    kind = kind if kind in {"user", "ai"} else "user"
    if not name:
        return back("/manage/folders")
    existing = await session.scalar(select(Tag).where(Tag.user_id == user.id, Tag.name == name))
    if existing:
        return RedirectResponse("/manage/folders?err=That+tag+already+exists.", status_code=303)
    session.add(Tag(user_id=user.id, name=name, kind=kind, description=description.strip()[:300]))
    await session.commit()
    return back("/manage/folders", "tag_saved")


@router.post("/tags/{tag_id}/update")
async def update_tag(
    request: Request,
    tag_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
):
    tag = await session.get(Tag, tag_id)
    if tag is None or tag.user_id != user.id:
        raise HTTPException(status_code=404)
    if name.strip():
        tag.name = name.strip()[:120]
    if kind in {"user", "ai"}:
        tag.kind = kind
    tag.description = description.strip()[:300]
    await session.commit()
    return back("/manage/folders", "tag_saved")


@router.post("/tags/{tag_id}/delete")
async def delete_tag(
    request: Request,
    tag_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    tag = await session.get(Tag, tag_id)
    if tag is None or tag.user_id != user.id:
        raise HTTPException(status_code=404)
    await session.delete(tag)
    await session.commit()
    return back("/manage/folders", "tag_removed")


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

RULE_FIELDS = {"title", "body", "author", "url"}
RULE_ACTIONS = {"hide", "star", "tag", "mark_read"}


@router.get("/rules")
async def rules_page(request: Request, user: CurrentUser, session: DB):
    rules = list(await session.scalars(select(FeedRule).where(FeedRule.user_id == user.id).order_by(FeedRule.created_at)))
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id).order_by(Feed.title)))
    feed_names = {f.id: f.title or f.url for f in feeds}
    return page(request, user, "rules", "manage/rules.html", {"rules": rules, "feeds": feeds, "feed_names": feed_names})


def _rule_values(feed_id: str, field: str, pattern: str, is_regex: str, action: str, action_value: str) -> dict:
    return {
        "feed_id": parse_uuid(feed_id),
        "field": field if field in RULE_FIELDS else "title",
        "pattern": pattern.strip()[:500],
        "is_regex": is_regex in {"1", "on", "true"},
        "action": action if action in RULE_ACTIONS else "hide",
        "action_value": action_value.strip()[:120] or None,
    }


@router.post("/rules")
async def create_rule(
    request: Request,
    user: CsrfUser,
    session: DB,
    feed_id: Annotated[str, Form()] = "",
    field: Annotated[str, Form()] = "title",
    pattern: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "hide",
    action_value: Annotated[str, Form()] = "",
):
    values = _rule_values(feed_id, field, pattern, is_regex, action, action_value)
    if not values["pattern"]:
        return back("/manage/rules")
    if values["feed_id"] is not None:
        await get_feed_or_404(session, user, values["feed_id"])
    session.add(FeedRule(user_id=user.id, **values))
    await session.commit()
    return back("/manage/rules", "rule_saved")


async def _get_rule(session: AsyncSession, user: User, rule_id: uuid.UUID) -> FeedRule:
    rule = await session.get(FeedRule, rule_id)
    if rule is None or rule.user_id != user.id:
        raise HTTPException(status_code=404)
    return rule


@router.post("/rules/{rule_id}/update")
async def update_rule(
    request: Request,
    rule_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    feed_id: Annotated[str, Form()] = "",
    field: Annotated[str, Form()] = "title",
    pattern: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "hide",
    action_value: Annotated[str, Form()] = "",
):
    rule = await _get_rule(session, user, rule_id)
    values = _rule_values(feed_id, field, pattern, is_regex, action, action_value)
    if values["pattern"]:
        for k, v in values.items():
            setattr(rule, k, v)
        await session.commit()
    return back("/manage/rules", "rule_saved")


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(
    request: Request,
    rule_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    rule = await _get_rule(session, user, rule_id)
    rule.enabled = not rule.enabled
    await session.commit()
    return back("/manage/rules", "rule_saved")


@router.post("/rules/{rule_id}/delete")
async def delete_rule(
    request: Request,
    rule_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    rule = await _get_rule(session, user, rule_id)
    await session.delete(rule)
    await session.commit()
    return back("/manage/rules", "rule_removed")


APPLY_RULE_MAX_ITEMS = 5000


async def apply_rule_to_existing(session: AsyncSession, user: User, rule: FeedRule) -> int:
    """Run one rule over the items it covers (its feed, or all the user's feeds) and merge into item_states."""
    from pensieve.fetch.subscribe import apply_rules  # type: ignore[import-not-found]

    stmt = (
        select(Item)
        .join(Feed, Feed.id == Item.feed_id)
        .where(Feed.user_id == user.id)
        .order_by(Item.published_at.desc())
        .limit(APPLY_RULE_MAX_ITEMS)
    )
    if rule.feed_id is not None:
        stmt = stmt.where(Item.feed_id == rule.feed_id)
    items = list(await session.scalars(stmt))
    now = datetime.now(UTC)
    matched = apply_rules([rule], user.id, items, now)
    if not matched:
        return 0
    existing = {
        st.item_id: st
        for st in await session.scalars(
            select(ItemState).where(ItemState.user_id == user.id, ItemState.item_id.in_([m.item_id for m in matched]))
        )
    }
    for new in matched:
        current = existing.get(new.item_id)
        if current is None:
            session.add(new)
            continue
        # Merge: rules only ever add state, never clear what the user set.
        if new.hidden:
            current.hidden = True
        if new.is_starred and not current.is_starred:
            current.is_starred, current.starred_at = True, now
        if new.is_read and not current.is_read:
            current.is_read, current.read_at = True, now
        if new.tags:
            current.tags = [*current.tags, *[t for t in new.tags if t not in current.tags]]
    await session.flush()
    invalidate_nav_cache(user.id)
    return len(matched)


@router.post("/rules/{rule_id}/apply")
async def apply_rule_route(
    request: Request,
    rule_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    rule = await _get_rule(session, user, rule_id)
    try:
        await apply_rule_to_existing(session, user, rule)
    except ImportError:
        return RedirectResponse("/manage/rules?err=Rules+can%27t+be+applied+retroactively+yet.", status_code=303)
    await session.commit()
    return back("/manage/rules", "rule_applied")


# ---------------------------------------------------------------------------
# AI and memory
# ---------------------------------------------------------------------------


def ai_settings(user: User) -> dict[str, Any]:
    s = user.settings or {}
    out = {k: bool(s.get(k, True)) for k in AI_TOGGLES}
    out["digest_time"] = str(s.get("digest_time") or f"{get_settings().digest_hour_local:02d}:{get_settings().digest_minute_local:02d}")
    return out


@router.get("/ai")
async def ai_page(request: Request, user: CurrentUser, session: DB):
    profile = await session.scalar(
        select(Profile).where(Profile.user_id == user.id).order_by(Profile.version.desc()).limit(1)
    )
    stats_rows = await session.execute(
        select(AIJob.status, func.count()).where(AIJob.user_id == user.id).group_by(AIJob.status)
    )
    stats = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    for status_, n in stats_rows:
        # "partial" (some steps failed on the last try) counts as failed for the reader's purposes
        key = "failed" if status_ == "partial" else status_
        stats[key] = stats.get(key, 0) + int(n)
    recent_failed = list(
        await session.scalars(
            select(AIJob)
            .where(
                AIJob.user_id == user.id,
                or_(
                    AIJob.status.in_(["failed", "partial"]),
                    and_(AIJob.status == "done", AIJob.last_error.is_not(None)),  # done-with-warning
                ),
            )
            .order_by(AIJob.created_at.desc())
            .limit(8)
        )
    )
    return page(
        request,
        user,
        "ai",
        "manage/ai.html",
        {"ai": ai_settings(user), "profile": profile, "stats": stats, "recent_failed": recent_failed},
    )


@router.get("/ai/gateway")
async def gateway_status(request: Request, user: CurrentUser):
    settings = get_settings()
    status_ = {"ok": False, "models": [], "error": None, "url": settings.llm_base_url}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(settings.llm_catalog_url)
            resp.raise_for_status()
            data = resp.json()
        models: list[str] = []
        if isinstance(data, dict):
            entries = data.get("models") or data.get("data") or []
        else:
            entries = data
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                name = entry.get("id") or entry.get("model_name") or entry.get("name")
                if name:
                    models.append(str(name))
            elif isinstance(entry, str):
                models.append(entry)
        status_.update(ok=True, models=models[:24])
    except Exception as exc:  # noqa: BLE001
        status_["error"] = exc.__class__.__name__
    from pensieve.ai import model_choice

    await model_choice.apply_overrides(force=True)
    choice = {
        "effective": model_choice.effective(),
        "overrides": model_choice.overrides(),
        "defaults": model_choice.env_defaults(),
        "ladder": model_choice.REASONING_LADDER,
        "fast_idx": model_choice.ladder_index(model_choice.effective()["fast_reasoning"]),
        "long_idx": model_choice.ladder_index(model_choice.effective()["long_reasoning"]),
        "can_edit": user.role == UserRole.admin,
    }
    # The pickers list every model the gateway knows plus whatever is chosen or configured, so a model the
    # catalog is lagging on (or one typed by hand) is never silently dropped from the form.
    options = list(status_["models"])
    for v in (*choice["effective"].values(), *choice["defaults"].values()):
        if isinstance(v, str) and v and v not in options and v not in model_choice.REASONING_LADDER:
            options.append(v)
    choice["options"] = options
    return render(request, "manage/gateway.html", {"gateway": status_, "choice": choice}, user=user)


@router.post("/ai/models")
async def save_models(request: Request, user: CsrfUser, session: DB):
    """Admin-only: choose which gateway models and reasoning efforts the whole install uses."""
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Only an admin can change the gateway models")
    from pensieve.ai import model_choice

    form = await request.form()
    values = model_choice.clean({k: form.get(k) for k in model_choice.FIELDS})
    await model_choice.save(session, values)
    await session.commit()
    if is_htmx(request):
        return await gateway_status(request, user)
    return back("/manage/ai", "models_saved")


@router.post("/ai/settings")
async def save_ai_settings(
    request: Request,
    user: CsrfUser,
    session: DB,
):
    form = await request.form()
    settings = dict(user.settings or {})
    for key in AI_TOGGLES:
        settings[key] = form.get(key) in {"1", "on", "true"}
    digest_time = str(form.get("digest_time") or "").strip()
    if digest_time:
        try:
            datetime.strptime(digest_time, "%H:%M").replace(tzinfo=UTC)
            settings["digest_time"] = digest_time
        except ValueError:
            pass
    db_user = await session.get(User, user.id)
    db_user.settings = settings
    await session.commit()
    return back("/manage/ai", "settings_saved")


@router.post("/ai/profile")
async def save_profile(
    request: Request,
    user: CsrfUser,
    session: DB,
    body_text: Annotated[str, Form()] = "",
):
    latest = await session.scalar(
        select(Profile).where(Profile.user_id == user.id).order_by(Profile.version.desc()).limit(1)
    )
    version = (latest.version + 1) if latest else 1
    session.add(
        Profile(
            user_id=user.id,
            version=version,
            body_text=body_text.strip(),
            diff_from_previous=None,
            edited_by_user=True,
        )
    )
    await session.commit()
    return back("/manage/ai", "profile_saved")


# ---------------------------------------------------------------------------
# Import and export
# ---------------------------------------------------------------------------


@router.get("/import")
async def import_page(request: Request, user: CurrentUser):
    return page(request, user, "import", "manage/import.html", {"result": None, "error": request.query_params.get("err")})


@router.post("/import/opml")
async def import_opml_route(
    request: Request,
    file: Annotated[UploadFile, File()],
    user: CsrfUser,
    session: DB,
):
    data = await file.read()
    try:
        from pensieve.fetch.opml import import_opml  # type: ignore[import-not-found]
    except ImportError:
        return page(request, user, "import", "manage/import.html", {"result": None, "error": "OPML import isn't available yet."}, 400)
    try:
        result = await import_opml(session, user, data)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        await session.refresh(user)
        return page(request, user, "import", "manage/import.html", {"result": None, "error": str(exc) or "Import failed."}, 400)
    return page(request, user, "import", "manage/import.html", {"result": result, "flash": FLASH["opml_imported"]})


@router.get("/export/opml")
async def export_opml_route(user: CurrentUser, session: DB):
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id).order_by(Feed.title)))
    folders = await user_folders(session, user)
    try:
        from pensieve.fetch.opml import export_opml  # type: ignore[import-not-found]

        data = export_opml(user, feeds, folders)
    except ImportError:
        data = _fallback_opml(feeds, folders)
    return Response(
        data, media_type="text/x-opml", headers={"Content-Disposition": 'attachment; filename="pensieve.opml"'}
    )


def _fallback_opml(feeds: list[Feed], folders: list[Folder]) -> bytes:
    from xml.sax.saxutils import quoteattr

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<opml version=\"2.0\"><head><title>Pensieve</title></head><body>"]
    by_folder: dict[uuid.UUID | None, list[Feed]] = {}
    for f in feeds:
        by_folder.setdefault(f.folder_id, []).append(f)

    def outline(f: Feed) -> str:
        return (
            f"<outline type=\"rss\" text={quoteattr(f.title or f.url)} title={quoteattr(f.title or f.url)} "
            f"xmlUrl={quoteattr(f.url)} htmlUrl={quoteattr(f.site_url or '')}/>"
        )

    for folder in folders:
        lines.append(f"<outline text={quoteattr(folder.name)} title={quoteattr(folder.name)}>")
        lines.extend(outline(f) for f in by_folder.get(folder.id, []))
        lines.append("</outline>")
    lines.extend(outline(f) for f in by_folder.get(None, []))
    lines.append("</body></opml>")
    return "\n".join(lines).encode()


@router.get("/export/json")
async def export_json(user: CurrentUser, session: DB):
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id)))
    folders = await user_folders(session, user)
    items = (
        await session.execute(
            select(Item, ItemState)
            .join(Feed, Feed.id == Item.feed_id)
            .outerjoin(ItemState, (ItemState.item_id == Item.id) & (ItemState.user_id == user.id))
            .where(Feed.user_id == user.id)
            .order_by(Item.published_at.desc())
        )
    ).all()
    notes = list(await session.scalars(select(Note).where(Note.user_id == user.id)))
    profile = await session.scalar(
        select(Profile).where(Profile.user_id == user.id).order_by(Profile.version.desc()).limit(1)
    )

    def dt(v: datetime | None) -> str | None:
        return v.isoformat() if v else None

    payload = {
        "exported_at": datetime.now(UTC).isoformat(),
        "user": {"email": user.email, "display_name": user.display_name},
        "folders": [{"id": str(f.id), "name": f.name, "position": f.position} for f in folders],
        "feeds": [
            {"id": str(f.id), "title": f.title, "url": f.url, "site_url": f.site_url, "folder_id": str(f.folder_id) if f.folder_id else None}
            for f in feeds
        ],
        "items": [
            {
                "id": str(i.id),
                "feed_id": str(i.feed_id),
                "title": i.title,
                "url": i.url,
                "author": i.author,
                "published_at": dt(i.published_at),
                "content_html": i.content_html,
                "state": {
                    "is_read": bool(s and s.is_read),
                    "is_starred": bool(s and s.is_starred),
                    "read_at": dt(s.read_at) if s else None,
                    "starred_at": dt(s.starred_at) if s else None,
                    "tags": list(s.tags) if s and s.tags else [],
                },
            }
            for i, s in items
        ],
        "notes": [
            {"id": str(n.id), "item_id": str(n.item_id), "quote": n.quote, "body": n.body, "created_at": dt(n.created_at)}
            for n in notes
        ],
        "profile": {"version": profile.version, "body_text": profile.body_text, "generated_at": dt(profile.generated_at)}
        if profile
        else None,
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="pensieve-export.json"'},
    )


# ---------------------------------------------------------------------------
# Account, tokens, users
# ---------------------------------------------------------------------------


@router.get("/account")
async def account_page(request: Request, user: CurrentUser):
    return page(request, user, "account", "manage/account.html", {"error": request.query_params.get("err")})


@router.post("/account/profile")
async def save_account(
    request: Request,
    user: CsrfUser,
    session: DB,
    display_name: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "auto",
):
    db_user = await session.get(User, user.id)
    if display_name.strip():
        db_user.display_name = display_name.strip()[:120]
    form = await request.form()
    settings = dict(db_user.settings or {})
    settings["theme"] = theme if theme in THEMES else "auto"
    for key, (choices, default) in READING_PREFS.items():
        value = str(form.get(key) or settings.get(key) or default)
        settings[key] = value if value in choices else default
    settings["auto_reader"] = str(form.get("auto_reader") or "") == "1"  # checkbox: absent means off
    db_user.settings = settings
    await session.commit()
    return back("/manage/account", "account_saved")


@router.post("/account/font")
async def save_font(request: Request, user: CsrfUser, session: DB):
    """Persist reading preferences changed live in the reader (the Aa popover and the +/- keys; 204).

    Accepts any subset of the READING_PREFS keys plus ``theme``; unknown values are ignored.
    """
    form = await request.form()
    db_user = await session.get(User, user.id)
    settings = dict(db_user.settings or {})
    for key, (choices, _default) in READING_PREFS.items():
        value = str(form.get(key) or "")
        if value in choices:
            settings[key] = value
    theme = str(form.get("theme") or "")
    if theme in THEMES:
        settings["theme"] = theme
    db_user.settings = settings
    await session.commit()
    return Response(status_code=204)


@router.post("/account/password")
async def change_password(
    request: Request,
    user: CsrfUser,
    session: DB,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
):
    db_user = await session.get(User, user.id)
    if not verify_password(current_password, db_user.password_hash):
        return RedirectResponse("/manage/account?err=Current+password+is+wrong.", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse("/manage/account?err=Use+at+least+8+characters.", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/manage/account?err=The+new+passwords+don%27t+match.", status_code=303)
    db_user.password_hash = hash_password(new_password)
    await session.commit()
    return back("/manage/account", "password_changed")


@router.get("/tokens")
async def tokens_page(request: Request, user: CurrentUser, session: DB):
    tokens = list(
        await session.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc()))
    )
    return page(request, user, "tokens", "manage/tokens.html", {"tokens": tokens, "new_token": None})


@router.post("/tokens")
async def create_token(
    request: Request,
    user: CsrfUser,
    session: DB,
    label: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "greader",
):
    kind = kind if kind in {"greader", "fever", "web"} else "greader"
    plaintext = generate_api_token()
    if kind == "fever":
        # Fever clients only ever send md5("email:password"); the sync API compares against that digest.
        try:
            from pensieve.syncapi.fever import fever_api_key_hash  # type: ignore[import-not-found]

            token_hash = fever_api_key_hash(user.email, plaintext)
        except ImportError:
            import hashlib

            token_hash = hashlib.md5(f"{user.email}:{plaintext}".encode(), usedforsecurity=False).hexdigest()
    else:
        token_hash = hash_api_token(plaintext)
    token = ApiToken(user_id=user.id, label=label.strip()[:120] or kind, kind=kind, token_hash=token_hash)
    session.add(token)
    await session.commit()
    tokens = list(
        await session.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc()))
    )
    return page(
        request,
        user,
        "tokens",
        "manage/tokens.html",
        {"tokens": tokens, "new_token": {"plaintext": plaintext, "label": token.label, "kind": kind}},
    )


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(
    request: Request,
    token_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    token = await session.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        raise HTTPException(status_code=404)
    token.revoked_at = datetime.now(UTC)
    await session.commit()
    return back("/manage/tokens", "token_revoked")


def require_admin(user: User) -> None:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin only")


@router.get("/users")
async def users_page(request: Request, user: CurrentUser, session: DB):
    require_admin(user)
    users = list(await session.scalars(select(User).order_by(User.created_at)))
    return page(request, user, "users", "manage/users.html", {"users": users, "invited": None, "error": request.query_params.get("err")})


@router.post("/users")
async def invite_user(
    request: Request,
    user: CsrfUser,
    session: DB,
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "reader",
):
    require_admin(user)
    email = email.strip().lower()
    if "@" not in email:
        return RedirectResponse("/manage/users?err=Enter+a+valid+email.", status_code=303)
    if await session.scalar(select(User).where(func.lower(User.email) == email)):
        return RedirectResponse("/manage/users?err=That+email+is+already+a+user.", status_code=303)
    temp_password = secrets.token_urlsafe(9)
    new_user = User(
        email=email,
        display_name=display_name.strip()[:120] or email.split("@")[0],
        password_hash=hash_password(temp_password),
        role=UserRole.admin if role == "admin" else UserRole.reader,
        settings={},
    )
    session.add(new_user)
    await session.commit()
    users = list(await session.scalars(select(User).order_by(User.created_at)))
    return page(
        request,
        user,
        "users",
        "manage/users.html",
        {"users": users, "invited": {"email": email, "password": temp_password}},
    )


async def _target_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    return target


@router.post("/users/{user_id}/role")
async def toggle_role(
    request: Request,
    user_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    require_admin(user)
    target = await _target_user(session, user_id)
    if target.id == user.id:
        return RedirectResponse("/manage/users?err=You+can%27t+change+your+own+role.", status_code=303)
    target.role = UserRole.reader if target.role == UserRole.admin else UserRole.admin
    await session.commit()
    return back("/manage/users", "user_saved")


@router.post("/users/{user_id}/ai")
async def toggle_ai(
    request: Request,
    user_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    require_admin(user)
    target = await _target_user(session, user_id)
    target.ai_enabled = not target.ai_enabled
    await session.commit()
    return back("/manage/users", "user_saved")


@router.post("/users/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
):
    require_admin(user)
    if user_id == user.id:
        return RedirectResponse("/manage/users?err=You+can%27t+delete+yourself.", status_code=303)
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()
    return back("/manage/users", "user_removed")
