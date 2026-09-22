"""The daily paper, the analytics digest, the weekly review and stored insights."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.ai import paper
from pensieve.ai.common import ai_on
from pensieve.models import Feed, Folder, Insight, Tag, User
from pensieve.web.queries import parse_uuid, set_read, user_owns_items
from pensieve.web.templating import DB, CsrfUser, CurrentUser, hx_trigger, is_htmx, render

log = logging.getLogger(__name__)
router = APIRouter()

PAPER_STALE = timedelta(minutes=10)
"""An edition older than this is recompiled when opened; compiling is a few queries, never a model call."""


async def latest(session: AsyncSession, user: User, kind: str) -> Insight | None:
    return await session.scalar(
        select(Insight)
        .where(Insight.user_id == user.id, Insight.kind == kind)
        .order_by(Insight.period.desc(), Insight.generated_at.desc())
        .limit(1)
    )


async def _history(session: AsyncSession, user: User, kinds: list[str], limit: int = 14) -> list[Insight]:
    return list(
        await session.scalars(
            select(Insight)
            .where(Insight.user_id == user.id, Insight.kind.in_(kinds))
            .order_by(Insight.period.desc(), Insight.kind)
            .limit(limit)
        )
    )


# ---------------------------------------------------------------------------
# Analytics digest and weekly review (unchanged shape; the model writes these at digest time)
# ---------------------------------------------------------------------------


async def _render_insight(request: Request, session: AsyncSession, user: User, insight: Insight | None, kind: str):
    if insight is not None and insight.opened_at is None:
        insight.opened_at = datetime.now(UTC)
        await session.commit()
    body = dict(insight.body or {}) if insight else {}
    ctx = {
        "insight": insight,
        "kind": kind,
        "body": body,
        "top_stories": body.get("top_stories") or [],
        "safe_to_skip": body.get("safe_to_skip") or {},
        "starred_unread": body.get("starred_unread") or [],
        "trends": body.get("trends") or [],
        "summary": body.get("summary") or "",
        "history": await _history(session, user, ["daily", "weekly"]),
        "queued": request.query_params.get("queued") == "1",
    }
    return render(request, "insights.html", ctx, partial="partials/insight_body.html", user=user)


@router.get("/insights/digest")
async def daily(request: Request, user: CurrentUser, session: DB):
    return await _render_insight(request, session, user, await latest(session, user, "daily"), "daily")


@router.get("/insights/weekly")
async def weekly(request: Request, user: CurrentUser, session: DB):
    return await _render_insight(request, session, user, await latest(session, user, "weekly"), "weekly")


# ---------------------------------------------------------------------------
# The paper
# ---------------------------------------------------------------------------


def _section_rows(config: dict[str, Any], vocab: list[str], folders: list[str], present: list[str]) -> list[dict]:
    """Rows for the layout editor: configured sections in their order, then every other candidate (auto)."""
    keys = folders if config["group_by"] == "folder" else vocab
    rows: list[dict] = []
    seen: set[str] = set()
    for entry in config["sections"]:
        rows.append({**entry, "title": paper.section_title(entry["key"]), "auto": False})
        seen.add(entry["key"])
    for key in [*present, *sorted(k.lower() for k in keys)]:
        key = key.lower()
        if key in seen or key == paper.OTHER_KEY:
            continue
        seen.add(key)
        rows.append({"key": key, "on": True, "limit": None, "title": paper.section_title(key), "auto": True})
    return rows


async def _paper_ctx(request: Request, session: AsyncSession, user: User, edition: Insight | None) -> dict[str, Any]:
    body = dict(edition.body or {}) if edition else {}
    config = paper.paper_config(user)
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id).order_by(Feed.title)))
    vocab = list(
        await session.scalars(select(Tag.name).where(Tag.user_id == user.id, Tag.kind == "ai").order_by(Tag.name))
    )
    folders = list(await session.scalars(select(Folder.name).where(Folder.user_id == user.id).order_by(Folder.name)))
    present = [s["key"] for s in body.get("sections") or []]
    return {
        "edition": edition,
        "kind": "paper",
        "body": body,
        "sections": body.get("sections") or [],
        "config": config,
        "section_rows": _section_rows(config, vocab, folders, present),
        "feeds": feeds,
        "muted": set(config["muted_feeds"]),
        "history": await _history(session, user, ["paper"]),
        "is_today": bool(edition and edition.period == paper.today().isoformat()),
        "paper_on": ai_on(user, "paper"),
        "summaries_on": ai_on(user, "summarize_items"),
        "queued": request.query_params.get("queued") == "1",
        "saved": request.query_params.get("saved") == "1",
    }


async def _fresh_edition(session: AsyncSession, user: User, *, force: bool = False) -> Insight | None:
    """Today's edition, recompiled when missing, stale or ``force``."""
    if not ai_on(user, "paper"):
        return await paper.get_edition(session, user, paper.today())
    edition = await paper.get_edition(session, user, paper.today())
    stale = edition is None or force or (datetime.now(UTC) - edition.generated_at) > PAPER_STALE
    if stale:
        edition = await paper.daily_paper(session, user, paper.today())
    if edition.opened_at is None:
        edition.opened_at = datetime.now(UTC)
    await session.commit()
    return edition


async def _render_paper(request: Request, session: AsyncSession, user: User, edition: Insight | None):
    ctx = await _paper_ctx(request, session, user, edition)
    return render(request, "insights.html", ctx, partial="partials/paper_body.html", user=user)


@router.get("/insights")
async def paper_today(request: Request, user: CurrentUser, session: DB):
    edition = await _fresh_edition(session, user, force=request.query_params.get("refresh") == "1")
    return await _render_paper(request, session, user, edition)


@router.get("/insights/{insight_id}")
async def by_id(request: Request, insight_id: uuid.UUID, user: CurrentUser, session: DB):
    insight = await session.get(Insight, insight_id)
    if insight is None or insight.user_id != user.id:
        raise HTTPException(status_code=404, detail="Insight not found")
    if insight.kind == paper.KIND:
        if insight.opened_at is None:
            insight.opened_at = datetime.now(UTC)
            await session.commit()
        return await _render_paper(request, session, user, insight)
    return await _render_insight(request, session, user, insight, insight.kind)


async def _edition_or_404(session: AsyncSession, user: User, insight_id: uuid.UUID) -> Insight:
    row = await session.get(Insight, insight_id)
    if row is None or row.user_id != user.id or row.kind != paper.KIND:
        raise HTTPException(status_code=404, detail="Edition not found")
    return row


def _find_story(body: dict[str, Any], key: str) -> tuple[dict | None, dict | None]:
    for section in body.get("sections") or []:
        for story in [*section.get("stories", []), *section.get("brief", [])]:
            if story.get("key") == key:
                return section, story
    return None, None


async def _save_paper_config(session: AsyncSession, user: User, config: dict[str, Any]) -> None:
    db_user = await session.get(User, user.id)
    settings = dict(db_user.settings or {})
    settings["paper"] = config
    db_user.settings = settings
    user.settings = settings
    await session.flush()


@router.post("/insights/paper/settings")
async def save_paper_settings(request: Request, user: CsrfUser, session: DB):
    form = await request.form()
    config = paper.config_from_form(form, paper.paper_config(user))
    await _save_paper_config(session, user, config)
    if ai_on(user, "paper"):
        await paper.daily_paper(session, user, paper.today(), config=config)
    await session.commit()
    return RedirectResponse("/insights?saved=1", status_code=303)


@router.post("/insights/paper/{insight_id}/hide")
async def hide_story(request: Request, insight_id: uuid.UUID, user: CsrfUser, session: DB, key: Annotated[str, Form()]):
    """Prune one story from this edition (kept out of it on recompiles)."""
    edition = await _edition_or_404(session, user, insight_id)
    body = dict(edition.body or {})
    hidden = list(body.get("hidden") or [])
    if key not in hidden:
        hidden.append(key)
    sections = []
    for section in body.get("sections") or []:
        section = dict(section)
        section["stories"] = [s for s in section["stories"] if s["key"] != key]
        section["brief"] = [s for s in section.get("brief", []) if s["key"] != key]
        section["count"] = len(section["stories"]) + len(section["brief"])
        section["unread"] = sum(1 for s in [*section["stories"], *section["brief"]] if not s["read"])
        if section["count"]:
            sections.append(section)
    body.update(hidden=hidden, sections=sections, story_count=max(0, int(body.get("story_count") or 1) - 1))
    edition.body = body
    await session.commit()
    if is_htmx(request):
        return Response(status_code=200, headers=hx_trigger("story-hidden"))
    return RedirectResponse(f"/insights/{edition.id}", status_code=303)


@router.post("/insights/paper/{insight_id}/read")
async def read_story(request: Request, insight_id: uuid.UUID, user: CsrfUser, session: DB, key: Annotated[str, Form()] = "", section: Annotated[str, Form()] = ""):
    """Mark one story (``key``) or a whole section (``section``) as read; returns the updated partial."""
    edition = await _edition_or_404(session, user, insight_id)
    body = dict(edition.body or {})
    targets: list[dict] = []
    hit_section: dict | None = None
    if key:
        hit_section, story = _find_story(body, key)
        if story is None:
            raise HTTPException(status_code=404, detail="Story not found")
        targets = [story]
    elif section:
        hit_section = next((s for s in body.get("sections") or [] if s["key"] == section), None)
        if hit_section is None:
            raise HTTPException(status_code=404, detail="Section not found")
        targets = [*hit_section["stories"], *hit_section.get("brief", [])]
    ids = [u for u in (parse_uuid(m["item_id"]) for s in targets for m in s["members"]) if u]
    owned = await user_owns_items(session, user.id, ids)
    await set_read(session, user.id, owned, True)
    for story in targets:
        story["read"] = True
        for m in story["members"]:
            m["read"] = True
    for sec in body.get("sections") or []:
        sec["unread"] = sum(1 for s in [*sec["stories"], *sec.get("brief", [])] if not s["read"])
    body["unread_count"] = max(0, int(body.get("unread_count") or 0) - len(owned))
    edition.body = body
    await session.commit()
    ctx = {"edition": edition, "config": paper.paper_config(user), "section": hit_section}
    if key:
        ctx["story"] = targets[0]
        return render(request, "partials/paper_story.html", ctx, user=user, headers=hx_trigger("counts-changed"))
    return render(request, "partials/paper_section.html", ctx, user=user, headers=hx_trigger("counts-changed"))


@router.post("/insights/paper/hide-section")
async def hide_section(request: Request, user: CsrfUser, session: DB, key: Annotated[str, Form()]):
    """Turn a section off in the layout (undo from the customize panel)."""
    config = paper.paper_config(user)
    key = key.strip().lower()
    found = False
    for entry in config["sections"]:
        if entry["key"] == key:
            entry["on"] = False
            found = True
    if not found and key:
        config["sections"].append({"key": key, "on": False, "limit": None})
    await _save_paper_config(session, user, config)
    if ai_on(user, "paper"):
        await paper.daily_paper(session, user, paper.today(), config=config)
    await session.commit()
    return RedirectResponse("/insights?saved=1", status_code=303)


@router.post("/insights/paper/mute-feed")
async def mute_feed(request: Request, user: CsrfUser, session: DB, feed_id: Annotated[str, Form()]):
    config = paper.paper_config(user)
    fid = parse_uuid(feed_id)
    if fid and str(fid) not in config["muted_feeds"]:
        config["muted_feeds"].append(str(fid))
    await _save_paper_config(session, user, config)
    if ai_on(user, "paper"):
        await paper.daily_paper(session, user, paper.today(), config=config)
    await session.commit()
    return RedirectResponse("/insights?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Generate / prune actions shared by the digest
# ---------------------------------------------------------------------------


@router.post("/insights/generate")
async def generate(request: Request, user: CsrfUser, session: DB, kind: Annotated[str, Form()] = "daily"):
    if kind == "paper":
        if ai_on(user, "paper"):
            await paper.daily_paper(session, user, paper.today())
            await session.commit()
        return RedirectResponse("/insights", status_code=303)
    job = queue.AI_WEEKLY_REVIEW if kind == "weekly" else queue.AI_DAILY_DIGEST
    try:
        # same day-/week-qualified id as the cron dispatcher, so a manual run never double-queues today's job
        from pensieve.ai.jobs import digest_job_id, weekly_job_id  # type: ignore[import-not-found]

        job_id = weekly_job_id(user.id) if kind == "weekly" else digest_job_id(user.id)
        await queue.enqueue(job, str(user.id), _job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enqueue %s: %s", job, exc)
    target = "/insights/weekly" if kind == "weekly" else "/insights/digest"
    return RedirectResponse(f"{target}?queued=1", status_code=303)


@router.post("/insights/{insight_id}/skip-read")
async def skip_read(request: Request, insight_id: uuid.UUID, user: CsrfUser, session: DB):
    insight = await session.get(Insight, insight_id)
    if insight is None or insight.user_id != user.id:
        raise HTTPException(status_code=404, detail="Insight not found")
    skip = (insight.body or {}).get("safe_to_skip") or {}
    ids = [u for u in (parse_uuid(x) for x in skip.get("item_ids") or []) if u]
    owned = await user_owns_items(session, user.id, ids)
    await set_read(session, user.id, owned, True)
    body = dict(insight.body or {})
    body["safe_to_skip"] = dict(skip, done=True, marked=len(owned))
    insight.body = body
    await session.commit()
    ctx = {"insight": insight, "safe_to_skip": body["safe_to_skip"], "marked": len(owned)}
    return render(request, "partials/safe_to_skip.html", ctx, user=user, headers=hx_trigger("counts-changed"))
