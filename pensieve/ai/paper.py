"""The daily paper: every story of the window (one row per cluster, not per copy), laid out in sections.

Unlike the digest in ``insights`` this needs no model call, so it is compiled on demand in well under a
second and is never waiting on the gateway; the summaries it shows were written eagerly when the items
arrived (``insights.summarize_items``). The reader's layout preferences live in ``user.settings["paper"]``
(see ``paper_config``); their prunes for one edition (hidden stories) live in the edition's body and are
carried over when the edition is recompiled.

Body shape of an ``Insight`` of kind ``paper``::

    {
      "sections": [{"key", "title", "kind": "tag"|"folder"|"other", "count", "unread",
                    "stories": [Story...], "brief": [Story...]}],
      "story_count", "item_count", "unread_count", "window": {"start", "end"},
      "hidden": ["c:<cluster_id>", "i:<item_id>"], "lede": str, "config": {...}
    }

A ``Story`` is ``{"key", "cluster_id", "item_id", "title", "sources", "feeds": [{"id", "title"}], "tags",
"tag", "published_at", "summary", "summary_item_id", "read", "members": [{"item_id", "title", "feed_id",
"feed", "published_at", "read"}], "folded": bool, "boost": float}``. ``brief`` holds the stories whose
*effective* sources (``sources + boost``) are under ``min_sources``.

Tuning ("more of this" / "less of this") lives in ``config["tuning"] = {"tags": {tag: w}, "feeds": {id: w}}``,
``w`` in [-3, 3]. A story's ``boost`` is its tags' weights (scaled by how strongly it carries each tag) plus the
mean weight of its feeds; it counts like extra (or missing) sources when ranking and when splitting main from
brief, and orders the automatic sections. Every tune is also recorded as a ``Correction`` so the profile refresh
sees it and the why-it-matters follows.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai.common import utcnow
from pensieve.ai.insights import _ai_rows, _hidden_ids, _items_between, _tz, _upsert_insight, digest_window

log = logging.getLogger(__name__)

KIND = "paper"
WINDOW_CHOICES = (24, 48, 72)
GROUP_CHOICES = ("tag", "folder")
DEFAULT_PER_SECTION = 8
MAX_PER_SECTION = 50
DEFAULT_MIN_SOURCES = 1
MAX_MIN_SOURCES = 10
OTHER_KEY = "other"
OTHER_TITLE = "Everything else"
TUNE_MAX = 3.0
TUNE_STEP_TAG = 1.0
TUNE_STEP_FEED = 0.5
TUNE_DIRECTIONS = ("more", "less", "reset")
_ACRONYMS = {"ai": "AI", "ml": "ML", "llm": "LLMs", "ios": "iOS", "macos": "macOS", "aws": "AWS", "gpu": "GPUs",
             "api": "APIs", "devops": "DevOps", "ux": "UX", "ui": "UI", "vr": "VR", "ar": "AR", "os": "OS",
             "db": "Databases", "k8s": "Kubernetes", "iot": "IoT", "cli": "CLI", "gpt": "GPT", "sql": "SQL",
             "javascript": "JavaScript", "typescript": "TypeScript", "github": "GitHub", "openai": "OpenAI",
             "nvidia": "NVIDIA", "macbook": "MacBook", "iphone": "iPhone", "ipad": "iPad"}  # fmt: skip


def section_title(key: str) -> str:
    """Display title for a tag or folder key: acronyms kept, hyphens to spaces, Title Case otherwise."""
    if key == OTHER_KEY:
        return OTHER_TITLE
    words = key.replace("-", " ").replace("_", " ").split()
    return " ".join(_ACRONYMS.get(w.lower(), w if w[:1].isupper() else w.capitalize()) for w in words) or key


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _nearest_window(value: Any) -> int:
    hours = _clamp(value, WINDOW_CHOICES[0], WINDOW_CHOICES[-1], WINDOW_CHOICES[0])
    return min(WINDOW_CHOICES, key=lambda w: abs(w - hours))


def paper_config(user: models.User | None) -> dict[str, Any]:
    """Normalised layout preferences from ``user.settings["paper"]`` with every key present."""
    raw = dict(((user.settings if user else None) or {}).get("paper") or {})
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw.get("sections") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        limit = entry.get("limit")
        sections.append(
            {
                "key": key,
                "on": bool(entry.get("on", True)),
                "limit": _clamp(limit, 1, MAX_PER_SECTION, 0) if limit not in (None, "", 0, "0") else None,
            }
        )
    muted = []
    for fid in raw.get("muted_feeds") or []:
        try:
            muted.append(str(uuid.UUID(str(fid))))
        except ValueError:
            continue
    tuning_raw = raw.get("tuning") if isinstance(raw.get("tuning"), dict) else {}
    tuning: dict[str, dict[str, float]] = {"tags": {}, "feeds": {}}
    for tag, w in (tuning_raw.get("tags") or {}).items():
        key = str(tag).strip().lower()
        weight = _clamp_weight(w)
        if key and weight:
            tuning["tags"][key] = weight
    for fid, w in (tuning_raw.get("feeds") or {}).items():
        try:
            key = str(uuid.UUID(str(fid)))
        except ValueError:
            continue
        weight = _clamp_weight(w)
        if weight:
            tuning["feeds"][key] = weight
    return {
        "group_by": raw.get("group_by") if raw.get("group_by") in GROUP_CHOICES else "tag",
        "window_hours": _nearest_window(raw.get("window_hours")),
        "per_section": _clamp(raw.get("per_section"), 1, MAX_PER_SECTION, DEFAULT_PER_SECTION),
        "min_sources": _clamp(raw.get("min_sources"), 1, MAX_MIN_SOURCES, DEFAULT_MIN_SOURCES),
        "show_summaries": bool(raw.get("show_summaries", True)),
        "hide_read": bool(raw.get("hide_read", False)),
        "auto_sections": bool(raw.get("auto_sections", True)),
        "sections": sections,
        "muted_feeds": muted,
        "tuning": tuning,
    }


def _clamp_weight(value: Any) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(w) or math.isinf(w):
        return 0.0
    return round(max(-TUNE_MAX, min(TUNE_MAX, w)), 2)


def config_from_form(form: Any, current: dict[str, Any]) -> dict[str, Any]:
    """Merge a submitted settings form into the stored config (ordered ``section`` fields carry the layout)."""
    out = dict(current)
    group_by = str(form.get("group_by") or current["group_by"])
    out["group_by"] = group_by if group_by in GROUP_CHOICES else current["group_by"]
    out["window_hours"] = _nearest_window(form.get("window_hours") or current["window_hours"])
    out["per_section"] = _clamp(form.get("per_section"), 1, MAX_PER_SECTION, current["per_section"])
    out["min_sources"] = _clamp(form.get("min_sources"), 1, MAX_MIN_SOURCES, current["min_sources"])
    for key in ("show_summaries", "hide_read", "auto_sections"):
        out[key] = str(form.get(key) or "") in {"1", "on", "true"}
    keys = [str(k).strip().lower() for k in form.getlist("section") if str(k).strip()]
    if keys:
        enabled = {str(k).strip().lower() for k in form.getlist("section_on")}
        sections = []
        for key in keys:
            limit = form.get(f"limit:{key}")
            sections.append(
                {
                    "key": key,
                    "on": key in enabled,
                    "limit": _clamp(limit, 1, MAX_PER_SECTION, 0) if limit not in (None, "", "0") else None,
                }
            )
        out["sections"] = sections
    muted = []
    for fid in form.getlist("muted_feed"):
        try:
            muted.append(str(uuid.UUID(str(fid))))
        except ValueError:
            continue
    out["muted_feeds"] = muted
    tuning = {"tags": dict(current["tuning"]["tags"]), "feeds": dict(current["tuning"]["feeds"])}
    for name in form:
        if name.startswith("tune_tag:"):
            tuning["tags"][name[len("tune_tag:") :]] = _clamp_weight(form.get(name))
        elif name.startswith("tune_feed:"):
            tuning["feeds"][name[len("tune_feed:") :]] = _clamp_weight(form.get(name))
    if str(form.get("reset_tuning") or "") in {"1", "on", "true"}:
        tuning = {"tags": {}, "feeds": {}}
    out["tuning"] = tuning
    return paper_config(models.User(settings={"paper": out}))


def story_boost(story: dict[str, Any], tuning: dict[str, dict[str, float]]) -> float:
    """Extra (or missing) sources a story earns from the reader's tuning: each tag's weight scaled by how
    strongly the story carries that tag (its heaviest tag counts fully), plus the mean weight of its feeds."""
    tag_weight = story.get("tag_weight") or {}
    peak = max(tag_weight.values(), default=0.0) or 1.0
    boost = 0.0
    for tag, w in tuning.get("tags", {}).items():
        if tag in tag_weight:
            boost += w * (tag_weight[tag] / peak)
    feed_ids = [f["id"] for f in story.get("feeds") or []]
    feed_ws = [tuning.get("feeds", {}).get(fid, 0.0) for fid in feed_ids]
    if feed_ws:
        boost += sum(feed_ws) / len(feed_ws)
    return round(boost, 2)


def apply_tune(config: dict[str, Any], story: dict[str, Any], direction: str) -> dict[str, Any]:
    """"more" / "less" of a story: step its main tag and every one of its feeds; "reset" clears them. Returns
    the changed config (normalised); the caller stores it and records the correction."""
    if direction not in TUNE_DIRECTIONS:
        raise ValueError(direction)
    tuning = {"tags": dict(config["tuning"]["tags"]), "feeds": dict(config["tuning"]["feeds"])}
    sign = {"more": 1.0, "less": -1.0, "reset": 0.0}[direction]
    tag = story.get("tag")
    feed_ids = [f["id"] for f in story.get("feeds") or []]
    if tag:
        tuning["tags"][tag] = 0.0 if not sign else tuning["tags"].get(tag, 0.0) + sign * TUNE_STEP_TAG
    for fid in feed_ids:
        tuning["feeds"][fid] = 0.0 if not sign else tuning["feeds"].get(fid, 0.0) + sign * TUNE_STEP_FEED
    return paper_config(models.User(settings={"paper": {**config, "tuning": tuning}}))


def tune_summary(config: dict[str, Any], story: dict[str, Any], feed_titles: dict[str, str] | None = None) -> str:
    """Human line for a story's tuning state, e.g. "AI +2 · Alpha +0.5"; empty when nothing is tuned."""
    parts = []
    tags = config["tuning"]["tags"]
    for tag in story.get("tags") or []:
        if tags.get(tag):
            parts.append(f"{section_title(tag)} {tags[tag]:+g}")
    feeds = config["tuning"]["feeds"]
    for f in story.get("feeds") or []:
        w = feeds.get(f["id"])
        if w:
            parts.append(f"{(feed_titles or {}).get(f['id'], f.get('title') or 'feed')} {w:+g}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def paper_window(day: date, hours: int) -> tuple[datetime, datetime]:
    """Same end rule as the digest (today ends now, a past day at its digest time), ``hours`` long."""
    _start, end = digest_window(day)
    return end - timedelta(hours=hours), end


def story_key(cluster_id: uuid.UUID | None, item_id: uuid.UUID) -> str:
    return f"c:{cluster_id}" if cluster_id else f"i:{item_id}"


async def _read_ids(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not item_ids:
        return set()
    stmt = select(models.ItemState.item_id).where(
        models.ItemState.user_id == user_id,
        models.ItemState.is_read.is_(True),
        models.ItemState.item_id.in_(item_ids),
    )
    return set((await session.scalars(stmt)).all())


async def _memberships(
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, models.Cluster]:
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(models.ClusterItem.item_id, models.Cluster)
            .join(models.Cluster, models.Cluster.id == models.ClusterItem.cluster_id)
            .where(models.Cluster.user_id == user_id, models.ClusterItem.item_id.in_(item_ids))
        )
    ).all()
    return {item_id: cluster for item_id, cluster in rows}


async def compile_paper(
    session: AsyncSession,
    user: models.User,
    day: date,
    *,
    config: dict[str, Any] | None = None,
    hidden: list[str] | None = None,
    lede: str = "",
) -> dict[str, Any]:
    """Build the edition body for ``day`` from what is in the database right now (no model calls)."""
    cfg = config or paper_config(user)
    start, end = paper_window(day, cfg["window_hours"])
    hidden_keys = list(dict.fromkeys(hidden or []))
    muted = {uuid.UUID(f) for f in cfg["muted_feeds"]}

    items = await _items_between(session, user.id, start, end)
    rule_hidden = await _hidden_ids(session, user.id, [i.id for i in items])
    items = [i for i in items if i.id not in rule_hidden and i.feed_id not in muted]
    ids = [i.id for i in items]
    feeds = {
        f.id: f
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    folders = {
        f.id: f.name
        for f in (await session.scalars(select(models.Folder).where(models.Folder.user_id == user.id))).all()
    }
    ai_rows = await _ai_rows(session, user.id, ids)
    read_ids = await _read_ids(session, user.id, ids)
    membership = await _memberships(session, user.id, ids)

    # one story per cluster (members limited to the window), plus one per standalone item
    groups: dict[uuid.UUID | None, list] = defaultdict(list)
    order: list[tuple[uuid.UUID | None, uuid.UUID]] = []
    for item in items:  # newest first
        cluster = membership.get(item.id)
        gid = cluster.id if cluster else item.id
        if gid not in groups:
            order.append((cluster.id if cluster else None, item.id))
        groups[gid].append(item)

    stories: list[dict[str, Any]] = []
    for cluster_id, first_id in order:
        members = groups[cluster_id or first_id]
        cluster = membership.get(members[0].id) if cluster_id else None
        rep = members[0]
        if cluster is not None and cluster.canonical_item_id in {m.id for m in members}:
            rep = next(m for m in members if m.id == cluster.canonical_item_id)
        key = story_key(cluster_id, rep.id)
        if key in hidden_keys:
            continue
        member_read = {m.id: m.id in read_ids for m in members}
        if cfg["hide_read"] and all(member_read.values()):
            continue
        tag_weight: Counter = Counter()
        for m in members:
            row = ai_rows.get(m.id)
            if row is None:
                continue
            for t in row.tags:
                tag_weight[t] += float((row.confidences or {}).get(t, 0.5)) + 0.01
        tags = [t for t, _ in tag_weight.most_common()]
        summary_item = next((m for m in [rep, *members] if (ai_rows.get(m.id) and ai_rows[m.id].summary)), None)
        feed_ids = list(dict.fromkeys(m.feed_id for m in members))
        stories.append(
            {
                "key": key,
                "cluster_id": str(cluster_id) if cluster_id else None,
                "item_id": str(rep.id),
                "feed_id": str(rep.feed_id),
                "title": (cluster.headline if cluster and cluster.headline else rep.title) or "(untitled)",
                "sources": len(feed_ids),
                "copies": len(members),
                "feeds": [
                    {"id": str(fid), "title": feeds[fid].title if fid in feeds else ""} for fid in feed_ids
                ],
                "tags": tags,
                "tag": tags[0] if tags else None,
                "tag_weight": {t: round(w, 3) for t, w in tag_weight.items()},
                "published_at": max(m.published_at for m in members).isoformat(),
                "summary": ai_rows[summary_item.id].summary if summary_item else None,
                "summary_item_id": str(summary_item.id) if summary_item else None,
                "read": all(member_read.values()),
                "boost": 0.0,
                "members": [
                    {
                        "item_id": str(m.id),
                        "title": m.title,
                        "feed_id": str(m.feed_id),
                        "feed": feeds[m.feed_id].title if m.feed_id in feeds else "",
                        "published_at": m.published_at.isoformat(),
                        "read": member_read[m.id],
                    }
                    for m in sorted(members, key=lambda m: m.published_at, reverse=True)
                ],
                "folded": False,
            }
        )

    for story in stories:
        story["boost"] = story_boost(story, cfg["tuning"])

    # section of a story: its heaviest tag among the enabled sections (tag mode) or its feed's folder
    configured = {s["key"]: s for s in cfg["sections"]}
    enabled_keys = [s["key"] for s in cfg["sections"] if s["on"]]
    disabled_keys = {s["key"] for s in cfg["sections"] if not s["on"]}

    def section_of(story: dict[str, Any]) -> str:
        if cfg["group_by"] == "folder":
            rep_feed = feeds.get(uuid.UUID(story["feed_id"]))
            name = folders.get(rep_feed.folder_id) if rep_feed and rep_feed.folder_id else None
            return name.lower() if name else OTHER_KEY
        for t in story["tags"]:
            if t in enabled_keys:
                return t
        for t in story["tags"]:
            if t not in disabled_keys:
                return t if cfg["auto_sections"] else OTHER_KEY
        return OTHER_KEY

    by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for story in stories:
        by_section[section_of(story)].append(story)

    def effective_sources(story: dict[str, Any]) -> float:
        return story["sources"] + story["boost"]

    def rank(story: dict[str, Any]) -> tuple:
        top = max(story["tag_weight"].values(), default=0.0)
        return (-effective_sources(story), -story["sources"], -story["copies"], -top)

    tag_tuning = cfg["tuning"]["tags"]
    ordered_keys = [k for k in enabled_keys if k in by_section]
    # automatic sections: the ones the reader asked for more of first, then alphabetical
    extra = sorted(
        (k for k in by_section if k not in configured and k != OTHER_KEY),
        key=lambda k: (-tag_tuning.get(k, 0.0), k),
    )
    if cfg["auto_sections"]:
        ordered_keys += [k for k in extra if k not in ordered_keys]
    else:
        for k in extra:
            by_section[OTHER_KEY].extend(by_section.pop(k))
    for k in disabled_keys:
        by_section.pop(k, None)
    if OTHER_KEY in by_section:
        ordered_keys.append(OTHER_KEY)

    sections: list[dict[str, Any]] = []
    for key in ordered_keys:
        # stable sort: ties on every rank key keep the newest story first
        rows = sorted(sorted(by_section.get(key, []), key=lambda s: s["published_at"], reverse=True), key=rank)
        if not rows:
            continue
        limit = (configured.get(key) or {}).get("limit") or cfg["per_section"]
        main = [s for s in rows if effective_sources(s) >= cfg["min_sources"]]
        brief = [s for s in rows if effective_sources(s) < cfg["min_sources"]]
        for n, s in enumerate(main):
            s["folded"] = n >= limit
        kind = "other" if key == OTHER_KEY else ("folder" if cfg["group_by"] == "folder" else "tag")
        sections.append(
            {
                "key": key,
                "title": section_title(key) if kind != "folder" else next(
                    (name for name in folders.values() if name.lower() == key), section_title(key)
                ),
                "kind": kind,
                "count": len(rows),
                "unread": sum(1 for s in rows if not s["read"]),
                "stories": main,
                "brief": brief,
            }
        )

    return {
        "sections": sections,
        "story_count": len(stories),
        "item_count": len(items),
        "unread_count": len(ids) - sum(1 for i in ids if i in read_ids),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "hours": cfg["window_hours"]},
        "hidden": hidden_keys,
        "lede": lede,
        "config": cfg,
        "compiled_at": utcnow().isoformat(),
    }


async def latest_lede(session: AsyncSession, user_id: uuid.UUID, day: date) -> str:
    """The digest's model-written day summary when one exists for ``day`` (the paper itself never waits)."""
    row = await session.scalar(
        select(models.Insight).where(
            models.Insight.user_id == user_id, models.Insight.kind == "daily", models.Insight.period == day.isoformat()
        )
    )
    return str((row.body or {}).get("summary") or "") if row else ""


async def get_edition(session: AsyncSession, user: models.User, day: date) -> models.Insight | None:
    return await session.scalar(
        select(models.Insight).where(
            models.Insight.user_id == user.id, models.Insight.kind == KIND, models.Insight.period == day.isoformat()
        )
    )


async def daily_paper(
    session: AsyncSession, user: models.User, day: date, *, config: dict[str, Any] | None = None
) -> models.Insight:
    """Compile (or recompile) the edition for ``day`` and store it; prunes made on the previous compile of the
    same day are kept."""
    previous = await get_edition(session, user, day)
    hidden = list((previous.body or {}).get("hidden") or []) if previous else []
    body = await compile_paper(
        session, user, day, config=config, hidden=hidden, lede=await latest_lede(session, user.id, day)
    )
    refs = [uuid.UUID(s["item_id"]) for sec in body["sections"] for s in sec["stories"]]
    title = f"The paper for {day.isoformat()}"
    row = await _upsert_insight(session, user.id, KIND, day.isoformat(), title, body, refs)
    if previous is not None and previous.opened_at is not None:
        row.opened_at = previous.opened_at
    return row


def today(user: models.User | None = None) -> date:
    return datetime.now(_tz()).date()


__all__ = [
    "GROUP_CHOICES",
    "KIND",
    "TUNE_DIRECTIONS",
    "WINDOW_CHOICES",
    "apply_tune",
    "compile_paper",
    "config_from_form",
    "daily_paper",
    "get_edition",
    "paper_config",
    "paper_window",
    "section_title",
    "story_boost",
    "story_key",
    "today",
    "tune_summary",
]
