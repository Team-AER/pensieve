"""The daily paper, the analytics digest, the weekly review and stored insights."""

from __future__ import annotations

import copy
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.ai import insights as ai_insights
from pensieve.ai import paper
from pensieve.ai.common import ai_on
from pensieve.ai.memory import record_correction
from pensieve.models import AIJob, Feed, Folder, Insight, ItemAI, Tag, User
from pensieve.web.queries import parse_uuid, set_read, user_owns_items
from pensieve.web.templating import DB, CsrfUser, CurrentUser, hx_trigger, is_htmx, render

log = logging.getLogger(__name__)
router = APIRouter()

PAPER_STALE = timedelta(minutes=10)
"""An edition older than this is recompiled when opened; compiling is a few queries, never a model call."""
SUMMARY_MAX_POLLS = 30  # x 2 s: give up after a minute and offer a retry instead of polling forever
MISSING_CHUNK = 40


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


async def _render_insight(
    request: Request, session: AsyncSession, user: User, insight: Insight | None, kind: str
):
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


def _section_rows(
    config: dict[str, Any], vocab: list[str], folders: list[str], present: list[str]
) -> list[dict]:
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


def _body(edition: Insight) -> dict[str, Any]:
    """A deep copy of the edition body: nested story dicts get edited in place, and SQLAlchemy only notices
    the change when the value it holds is left untouched."""
    return copy.deepcopy(edition.body or {})


def _stories(body: dict[str, Any]) -> list[dict]:
    return [s for sec in body.get("sections") or [] for s in [*sec.get("stories", []), *sec.get("brief", [])]]


def _missing_summaries(body: dict[str, Any]) -> int:
    return sum(1 for story in _stories(body) if not story.get("summary"))


def _tune_lines(config: dict[str, Any], body: dict[str, Any], feeds: dict[str, str]) -> dict[str, str]:
    """story key -> its tuning line ("AI +1 · Alpha +0.5"), for every story of the edition."""
    out: dict[str, str] = {}
    for story in _stories(body):
        line = paper.tune_summary(config, story, feeds)
        if line:
            out[story["key"]] = line
    return out


def _tuned_rows(config: dict[str, Any], feeds: list[Feed]) -> dict[str, list[dict]]:
    """Current tuning for the Customize panel: every tuned tag and feed with its weight."""
    titles = {str(f.id): (f.title or f.url) for f in feeds}
    tags = [
        {"key": k, "title": paper.section_title(k), "weight": w}
        for k, w in sorted(config["tuning"]["tags"].items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    feed_rows = [
        {"key": k, "title": titles.get(k, "(removed feed)"), "weight": w}
        for k, w in sorted(config["tuning"]["feeds"].items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {"tags": tags, "feeds": feed_rows}


async def _paper_ctx(
    request: Request, session: AsyncSession, user: User, edition: Insight | None
) -> dict[str, Any]:
    body = dict(edition.body or {}) if edition else {}
    config = paper.paper_config(user)
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id).order_by(Feed.title)))
    vocab = list(
        await session.scalars(
            select(Tag.name).where(Tag.user_id == user.id, Tag.kind == "ai").order_by(Tag.name)
        )
    )
    folders = list(
        await session.scalars(select(Folder.name).where(Folder.user_id == user.id).order_by(Folder.name))
    )
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
        "summary_prefs": ai_insights.summary_prefs(user),
        "tuned": _tuned_rows(config, feeds),
        "tune_lines": _tune_lines(config, body, {str(f.id): (f.title or f.url) for f in feeds}),
        "missing_summaries": _missing_summaries(body),
        "queued": request.query_params.get("queued") == "1",
        "saved": request.query_params.get("saved") == "1",
        "summaries_queued": request.query_params.get("summaries") or "",
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


async def _save_summary_prefs(session: AsyncSession, user: User, prefs: dict[str, Any]) -> None:
    db_user = await session.get(User, user.id)
    settings = dict(db_user.settings or {})
    settings["summaries"] = prefs
    db_user.settings = settings
    user.settings = settings
    await session.flush()


@router.post("/insights/paper/settings")
async def save_paper_settings(request: Request, user: CsrfUser, session: DB):
    form = await request.form()
    config = paper.config_from_form(form, paper.paper_config(user))
    await _save_paper_config(session, user, config)
    prefs = ai_insights.summary_prefs_from_form(form, ai_insights.summary_prefs(user))
    await _save_summary_prefs(session, user, prefs)
    if ai_on(user, "paper"):
        await paper.daily_paper(session, user, paper.today(), config=config)
    await session.commit()
    return RedirectResponse("/insights?saved=1", status_code=303)


async def _feed_titles(session: AsyncSession, user: User) -> dict[str, str]:
    rows = await session.execute(select(Feed.id, Feed.title).where(Feed.user_id == user.id))
    return {str(fid): title for fid, title in rows.all()}


def _story_ctx(
    user: User, edition: Insight, section: dict | None, story: dict, feeds: dict[str, str]
) -> dict:
    config = paper.paper_config(user)
    return {
        "edition": edition,
        "config": config,
        "section": section,
        "story": story,
        "summary_prefs": ai_insights.summary_prefs(user),
        "tune_line": paper.tune_summary(config, story, feeds),
        "hx_open": True,  # the reader acted inside the expanded story: the swapped-in row must stay open
    }


def _summary_ctx(
    user: User, edition: Insight, story: dict, *, pending: bool, error: str | None, n: int
) -> dict:
    return {
        "edition": edition,
        "story": story,
        "pending": pending,
        "error": error,
        "n": n,
        "config": paper.paper_config(user),
        "summary_prefs": ai_insights.summary_prefs(user),
    }


def _story_or_404(body: dict[str, Any], key: str) -> tuple[dict | None, dict]:
    section, story = _find_story(body, key)
    if story is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return section, story


@router.post("/insights/paper/{insight_id}/tune")
async def tune_story(
    request: Request,
    insight_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    key: Annotated[str, Form()],
    direction: Annotated[str, Form()],
):
    """ "More of this" / "less of this" / reset for one story: steps its tag and feeds in the reader's tuning,
    records a correction for the profile, and recompiles today's edition so the next open reflects it."""
    edition = await _edition_or_404(session, user, insight_id)
    if direction not in paper.TUNE_DIRECTIONS:
        raise HTTPException(status_code=400, detail="Unknown direction")
    body = _body(edition)
    section, story = _story_or_404(body, key)
    config = paper.apply_tune(paper.paper_config(user), story, direction)
    await _save_paper_config(session, user, config)
    feeds = await _feed_titles(session, user)
    names = [story.get("tag") or "", *[feeds.get(f["id"], "") for f in story.get("feeds") or []]]
    what = ", ".join(x for x in names if x)
    await record_correction(
        session,
        user,
        "paper_story",
        story["item_id"],
        "preference",
        None if direction == "reset" else story.get("title"),
        f"{direction} of: {what}" if direction != "reset" else f"reset: {what}",
    )
    story["boost"] = paper.story_boost(story, config["tuning"])
    edition.body = body
    if ai_on(user, "paper") and edition.period == paper.today().isoformat():
        await paper.daily_paper(session, user, paper.today(), config=config)
    await session.commit()
    ctx = _story_ctx(user, edition, section, story, feeds)
    if is_htmx(request):
        headers = hx_trigger(f"paper-tuned-{direction}")
        return render(request, "partials/paper_story.html", ctx, user=user, headers=headers)
    return RedirectResponse(f"/insights/{edition.id}", status_code=303)


@router.post("/insights/paper/{insight_id}/rewrite")
async def rewrite_summary(
    request: Request,
    insight_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    key: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
):
    """The reader rejects a story's summary (usually its why-it-matters): record what was wrong and queue a
    rewrite that carries their note. Returns the story's summary block, polling until the rewrite lands."""
    edition = await _edition_or_404(session, user, insight_id)
    body = _body(edition)
    _section, story = _story_or_404(body, key)
    item_id = parse_uuid(story.get("summary_item_id") or story["item_id"])
    if item_id is None or not await user_owns_items(session, user.id, [item_id]):
        raise HTTPException(status_code=404, detail="Item not found")
    note = " ".join(note.split())[:400]
    old = story.get("summary") or None
    await record_correction(
        session, user, "item_summary", item_id, "why_it_matters", old, note or "off-target"
    )
    error = None
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    try:
        await queue.enqueue(
            queue.AI_SUMMARIZE_ITEM,
            str(user.id),
            str(item_id),
            note,
            _job_id=queue.job_id_for(queue.AI_SUMMARIZE_ITEM, f"{item_id}:rewrite:{stamp}"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enqueue rewrite for %s: %s", item_id, exc)
        error = "The AI queue is unavailable right now."
    story["summary"] = None
    story["summary_item_id"] = str(item_id)
    story["rewrite_of"] = old
    edition.body = body
    await session.commit()
    ctx = _summary_ctx(user, edition, story, pending=error is None, error=error, n=1)
    return render(request, "partials/paper_summary.html", ctx, user=user)


@router.post("/insights/paper/{insight_id}/summarize")
async def summarize_story(
    request: Request, insight_id: uuid.UUID, user: CsrfUser, session: DB, key: Annotated[str, Form()]
):
    """ "Summarize now" for one story of the paper (its representative item); polls the paper-native block."""
    edition = await _edition_or_404(session, user, insight_id)
    body = _body(edition)
    _section, story = _story_or_404(body, key)
    item_id = parse_uuid(story["item_id"])
    if item_id is None or not await user_owns_items(session, user.id, [item_id]):
        raise HTTPException(status_code=404, detail="Item not found")
    ai = await session.get(ItemAI, (user.id, item_id))
    error = None
    if not (ai and ai.summary):
        try:
            job_id = queue.job_id_for(queue.AI_SUMMARIZE_ITEM, item_id)
            await queue.enqueue(queue.AI_SUMMARIZE_ITEM, str(user.id), str(item_id), _job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enqueue summary for %s: %s", item_id, exc)
            error = "The AI queue is unavailable right now."
    else:
        story["summary"], story["summary_item_id"] = ai.summary, str(item_id)
        story.pop("rewrite_of", None)
        edition.body = body
        await session.commit()
    pending = not (ai and ai.summary) and not error
    ctx = _summary_ctx(user, edition, story, pending=pending, error=error, n=1)
    return render(request, "partials/paper_summary.html", ctx, user=user)


@router.get("/insights/paper/{insight_id}/summary")
async def poll_story_summary(
    request: Request, insight_id: uuid.UUID, user: CurrentUser, session: DB, key: str, n: int = 0
):
    """Polled every 2 s by the paper's summary block until the queued summary or rewrite has landed."""
    edition = await _edition_or_404(session, user, insight_id)
    body = _body(edition)
    _section, story = _story_or_404(body, key)
    item_id = parse_uuid(story.get("summary_item_id") or story["item_id"])
    ai = await session.get(ItemAI, (user.id, item_id)) if item_id else None
    error = None
    ready = bool(ai and ai.summary and ai.summary != story.get("rewrite_of"))
    if ready:
        story["summary"], story["summary_item_id"] = ai.summary, str(item_id)
        story.pop("rewrite_of", None)
        edition.body = body
        await session.commit()
    else:
        job = await session.scalar(
            select(AIJob)
            .where(AIJob.kind == "summarize", AIJob.target_id == item_id, AIJob.user_id == user.id)
            .order_by(AIJob.created_at.desc())
            .limit(1)
        )
        recent = (
            job is not None
            and job.finished_at
            and (datetime.now(UTC) - job.finished_at) < timedelta(minutes=5)
        )
        if job is not None and job.status in {"failed", "partial"} and recent:
            error = "The summary failed: " + (job.last_error or "the AI gateway did not answer.")[:160]
        elif n >= SUMMARY_MAX_POLLS:
            error = "The summary is taking longer than usual."
    ctx = _summary_ctx(user, edition, story, pending=not ready and not error, error=error, n=n + 1)
    return render(request, "partials/paper_summary.html", ctx, user=user)


@router.post("/insights/paper/{insight_id}/summaries")
async def write_missing_summaries(request: Request, insight_id: uuid.UUID, user: CsrfUser, session: DB):
    """Queue eager summaries for every story of this edition that has none (the manual retry)."""
    edition = await _edition_or_404(session, user, insight_id)
    body = _body(edition)
    ids = [u for u in (parse_uuid(s["item_id"]) for s in _stories(body) if not s.get("summary")) if u]
    owned = await user_owns_items(session, user.id, ids)
    queued = 0
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    for n, start in enumerate(range(0, len(owned), MISSING_CHUNK)):
        chunk = [str(i) for i in owned[start : start + MISSING_CHUNK]]
        try:
            job_id = queue.job_id_for(queue.AI_SUMMARIZE_ITEMS, f"missing:{edition.id}:{stamp}:{n}")
            await queue.enqueue(queue.AI_SUMMARIZE_ITEMS, str(user.id), chunk, _job_id=job_id)
            queued += len(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enqueue missing summaries: %s", exc)
            break
    return RedirectResponse(f"/insights/{edition.id}?summaries={queued}", status_code=303)


@router.post("/insights/paper/{insight_id}/hide")
async def hide_story(
    request: Request, insight_id: uuid.UUID, user: CsrfUser, session: DB, key: Annotated[str, Form()]
):
    """Prune one story from this edition (kept out of it on recompiles)."""
    edition = await _edition_or_404(session, user, insight_id)
    body = _body(edition)
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
async def read_story(
    request: Request,
    insight_id: uuid.UUID,
    user: CsrfUser,
    session: DB,
    key: Annotated[str, Form()] = "",
    section: Annotated[str, Form()] = "",
):
    """Mark one story (``key``) or a whole section (``section``) as read; returns the updated partial."""
    edition = await _edition_or_404(session, user, insight_id)
    body = _body(edition)
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
    feeds = await _feed_titles(session, user)
    config = paper.paper_config(user)
    ctx = {
        "edition": edition,
        "config": config,
        "section": hit_section,
        "summary_prefs": ai_insights.summary_prefs(user),
        "tune_lines": _tune_lines(config, body, feeds),
    }
    if key:
        ctx.update(_story_ctx(user, edition, hit_section, targets[0], feeds))
        return render(
            request, "partials/paper_story.html", ctx, user=user, headers=hx_trigger("counts-changed")
        )
    return render(
        request, "partials/paper_section.html", ctx, user=user, headers=hx_trigger("counts-changed")
    )


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
