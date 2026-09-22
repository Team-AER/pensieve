"""Daily digest, weekly review, per-item summary.

Approximations (documented here and in ``memory``):

* *opened* = marked read outside a mark-all batch; *skipped* = marked read inside one
  (batch = >= 5 item_states with the same read_at second, ``common.MARK_ALL_BATCH_MIN``).
* *open-rate* of a feed = opened items / items published, both over the last 30 days. Feeds with fewer than
  ``MIN_ITEMS_FOR_OPEN_RATE`` items in that window have an unknown open-rate and are never "safe to skip".
* *affinity* = cosine(item embedding, centroid of the user's starred + opened embeddings from the last 30 days).
  Without embeddings (no centroid or no item vector) it is a blend of the item's feed open-rate and the overlap
  between the item's AI tags and the user's top tags from opened + starred history, each 0..1; 0.5 only when the
  user has no history at all. +0.15 when one of the item's AI tags is mentioned in the profile text.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import prompts
from pensieve.ai.client import LLMClient, LLMError, get_client, validate_schema
from pensieve.ai.common import (
    ai_on,
    centroid,
    cosine,
    counter_cosine,
    is_batch_read,
    mark_all_batch_times,
    read_states,
    user_feed_ids,
    utcnow,
)
from pensieve.ai.embeddings import get_vectors
from pensieve.ai.memory import profile_text
from pensieve.config import get_settings

log = logging.getLogger(__name__)

AFFINITY_DAYS = 30
OPEN_RATE_DAYS = 30
MIN_ITEMS_FOR_OPEN_RATE = 5
SKIP_OPEN_RATE = 0.15
LOW_AFFINITY = 0.5
PROFILE_TAG_BOOST = 0.15
DEFAULT_AFFINITY = 0.5
TOP_STORIES = 8
STARRED_UNREAD_DAYS = 14
TREND_WEEKS = 4
TREND_TAGS = 8
SUMMARY_TEXT_CHARS = 24_000
SUMMARY_BATCH = 4
SUMMARY_BATCH_TEXT_CHARS = 6_000  # per article inside a batch; four leads fit the short-model input budget
SUMMARY_TOKENS_PER_ITEM = 260
SUMMARY_TOKENS_HEADROOM = 200
SUMMARY_RETRY_BATCH = 2  # entries the model dropped or mangled get one more, smaller batch
SUMMARY_FOCUS_CHARS = 400
TOP_TAGS = 20
TOP_TAGS_DAYS = 90


class ItemRef(NamedTuple):
    """The four item columns the digest and review need; loading full rows for a month of items is wasteful."""

    id: uuid.UUID
    feed_id: uuid.UUID
    published_at: datetime
    title: str


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001 - bad tz name in config must not break digests
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Reader-state helpers
# ---------------------------------------------------------------------------


async def opened_and_skipped(
    session: AsyncSession, user_id: uuid.UUID, since: datetime
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    batches = await mark_all_batch_times(session, user_id, since)
    opened: set[uuid.UUID] = set()
    skipped: set[uuid.UUID] = set()
    for s in await read_states(session, user_id, since):
        (skipped if is_batch_read(s, batches) else opened).add(s.item_id)
    return opened, skipped


async def _items_between(
    session: AsyncSession, user_id: uuid.UUID, start: datetime, end: datetime
) -> list[ItemRef]:
    stmt = (
        select(models.Item.id, models.Item.feed_id, models.Item.published_at, models.Item.title)
        .where(
            models.Item.feed_id.in_(user_feed_ids(user_id)),
            models.Item.published_at >= start,
            models.Item.published_at < end,
        )
        .order_by(models.Item.published_at.desc())
    )
    return [ItemRef(*row) for row in (await session.execute(stmt)).all()]


async def _hidden_ids(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not item_ids:
        return set()
    stmt = select(models.ItemState.item_id).where(
        models.ItemState.user_id == user_id,
        models.ItemState.hidden.is_(True),
        models.ItemState.item_id.in_(item_ids),
    )
    return set((await session.scalars(stmt)).all())


async def _ai_rows(
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, models.ItemAI]:
    if not item_ids:
        return {}
    rows = (
        await session.scalars(
            select(models.ItemAI).where(models.ItemAI.user_id == user_id, models.ItemAI.item_id.in_(item_ids))
        )
    ).all()
    return {r.item_id: r for r in rows}


async def feed_open_rates(session: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, float | None]:
    """Per feed: opened / published over the last 30 days; None when too few items to judge."""
    since = utcnow() - timedelta(days=OPEN_RATE_DAYS)
    items = await _items_between(session, user_id, since, utcnow() + timedelta(days=1))
    opened, _ = await opened_and_skipped(session, user_id, since)
    total: Counter = Counter()
    hits: Counter = Counter()
    for it in items:
        total[it.feed_id] += 1
        if it.id in opened:
            hits[it.feed_id] += 1
    return {fid: (hits[fid] / n if n >= MIN_ITEMS_FOR_OPEN_RATE else None) for fid, n in total.items()}


async def profile_centroid(session: AsyncSession, user_id: uuid.UUID) -> list[float] | None:
    since = utcnow() - timedelta(days=AFFINITY_DAYS)
    opened, _ = await opened_and_skipped(session, user_id, since)
    starred = set(
        (
            await session.scalars(
                select(models.ItemState.item_id).where(
                    models.ItemState.user_id == user_id,
                    models.ItemState.is_starred.is_(True),
                    models.ItemState.starred_at >= since,
                )
            )
        ).all()
    )
    ids = list(opened | starred)
    if not ids:
        return None
    vectors = await get_vectors(session, ids[:500])
    return centroid(list(vectors.values()))


async def top_tags(session: AsyncSession, user_id: uuid.UUID, days: int = TOP_TAGS_DAYS) -> dict[str, float]:
    """AI tags of what the user opened or starred in the last ``days``, weighted 0..1 (most frequent = 1)."""
    since = utcnow() - timedelta(days=days)
    opened, _ = await opened_and_skipped(session, user_id, since)
    starred = set(
        (
            await session.scalars(
                select(models.ItemState.item_id).where(
                    models.ItemState.user_id == user_id,
                    models.ItemState.is_starred.is_(True),
                    models.ItemState.starred_at >= since,
                )
            )
        ).all()
    )
    ids = list(opened | starred)
    if not ids:
        return {}
    counts: Counter = Counter()
    for row in (await _ai_rows(session, user_id, ids[:2000])).values():
        counts.update(row.tags)
    if not counts:
        return {}
    top = counts.most_common(TOP_TAGS)
    peak = float(top[0][1])
    return {name: round(n / peak, 4) for name, n in top}


async def tag_trends(session: AsyncSession, user_id: uuid.UUID, weeks: int = TREND_WEEKS) -> list[dict]:
    """Weekly counts of AI tags over the last ``weeks`` weeks (oldest first) and last-vs-previous delta."""
    now = utcnow()
    start = now - timedelta(weeks=weeks)
    items = await _items_between(session, user_id, start, now + timedelta(days=1))
    ai_rows = await _ai_rows(session, user_id, [i.id for i in items])
    series: dict[str, list[int]] = defaultdict(lambda: [0] * weeks)
    for it in items:
        row = ai_rows.get(it.id)
        if not row:
            continue
        bucket = min(weeks - 1, int((it.published_at - start).total_seconds() // (7 * 86400)))
        for tag in row.tags:
            series[tag][bucket] += 1
    ranked = sorted(series.items(), key=lambda kv: -sum(kv[1]))[:TREND_TAGS]
    return [
        {"name": name, "series": counts, "delta": counts[-1] - (counts[-2] if weeks > 1 else 0)}
        for name, counts in ranked
    ]


async def _upsert_insight(
    session: AsyncSession, user_id: uuid.UUID, kind: str, period: str, title: str, body: dict, item_refs: list
) -> models.Insight:
    row = await session.scalar(
        select(models.Insight).where(
            models.Insight.user_id == user_id, models.Insight.kind == kind, models.Insight.period == period
        )
    )
    if row is None:
        row = models.Insight(user_id=user_id, kind=kind, period=period)
        session.add(row)
    row.title = title
    row.body = body
    row.item_refs = list(item_refs)
    row.generated_at = utcnow()
    row.opened_at = None
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


def digest_window(day: date) -> tuple[datetime, datetime]:
    """The 24h the digest covers.

    For today (in the configured timezone) it ends *now*, so a manual run at 15:00 still covers the last 24h.
    For a past day it ends at that day's digest time.
    """
    s = get_settings()
    now = utcnow().astimezone(_tz())
    if day >= now.date():
        end = now
    else:
        end = datetime.combine(day, time(s.digest_hour_local, s.digest_minute_local), tzinfo=_tz())
    return end - timedelta(hours=24), end


async def daily_digest(
    session: AsyncSession, user: models.User, day: date, client: LLMClient | None = None
) -> models.Insight | None:
    if not ai_on(user, "digest"):
        return None
    client = client or get_client()
    settings = get_settings()
    start, end = digest_window(day)
    period = day.isoformat()

    items = await _items_between(session, user.id, start, end)
    hidden = await _hidden_ids(session, user.id, [i.id for i in items])
    items = [i for i in items if i.id not in hidden]
    feeds = {
        f.id: f
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    ai_rows = await _ai_rows(session, user.id, [i.id for i in items])
    vectors = await get_vectors(session, [i.id for i in items])
    center = await profile_centroid(session, user.id)
    profile = await profile_text(session, user.id)
    profile_lc = profile.lower()
    open_rates = await feed_open_rates(session, user.id)
    liked_tags = (
        await top_tags(session, user.id) if (center is None or len(vectors) < len(items)) else {}
    )
    has_history = bool(liked_tags) or any(r is not None for r in open_rates.values())

    # cluster membership for the window's items
    membership: dict[uuid.UUID, models.Cluster] = {}
    if items:
        rows = (
            await session.execute(
                select(models.ClusterItem.item_id, models.Cluster)
                .join(models.Cluster, models.Cluster.id == models.ClusterItem.cluster_id)
                .where(
                    models.Cluster.user_id == user.id, models.ClusterItem.item_id.in_([i.id for i in items])
                )
            )
        ).all()
        membership = {item_id: cluster for item_id, cluster in rows}

    def affinity(item: ItemRef) -> float:
        vec = vectors.get(item.id)
        tags = ai_rows[item.id].tags if item.id in ai_rows else []
        if vec is not None and center is not None:
            base = cosine(vec, center)
        elif has_history:
            # no embeddings: how often the reader opens this source, blended with tag overlap against history
            rate = open_rates.get(item.feed_id)
            rate_score = DEFAULT_AFFINITY if rate is None else rate
            tag_score = max((liked_tags.get(t, 0.0) for t in tags), default=0.0)
            base = 0.5 * rate_score + 0.5 * tag_score
        else:
            base = DEFAULT_AFFINITY
        if profile_lc and any(t.lower() in profile_lc for t in tags):
            base = min(1.0, base + PROFILE_TAG_BOOST)
        return round(base, 4)

    # group into entries: one per cluster (canonical or newest member) + standalone items
    entries: list[dict] = []
    seen_clusters: set[uuid.UUID] = set()
    for item in items:
        cluster = membership.get(item.id)
        if cluster is not None:
            if cluster.id in seen_clusters:
                continue
            seen_clusters.add(cluster.id)
            members = [
                i for i in items if membership.get(i.id) is not None and membership[i.id].id == cluster.id
            ]
            rep = next((i for i in members if i.id == cluster.canonical_item_id), members[0])
            aff = max(affinity(m) for m in members)
            sources = max(cluster.source_count, len({m.feed_id for m in members}))
            feed_ids = {m.feed_id for m in members}
            member_ids = [m.id for m in members]
            title = cluster.headline or rep.title
            cluster_id = cluster.id
        else:
            rep, aff, sources, feed_ids, member_ids, title, cluster_id = (
                item,
                affinity(item),
                1,
                {item.feed_id},
                [item.id],
                item.title,
                None,
            )
        tags = ai_rows[rep.id].tags if rep.id in ai_rows else []
        rates = [open_rates.get(f) for f in feed_ids]
        low_open = bool(rates) and all(r is not None and r < SKIP_OPEN_RATE for r in rates)
        entries.append(
            {
                "cluster_id": cluster_id,
                "item_id": rep.id,
                "title": title,
                "sources": sources,
                "tag": tags[0] if tags else None,
                "tags": tags,
                "affinity": aff,
                "score": round(sources * aff, 4),
                "feed": feeds[rep.feed_id].title if rep.feed_id in feeds else "",
                "member_ids": member_ids,
                "skip": low_open and aff <= LOW_AFFINITY,
            }
        )
    entries.sort(key=lambda e: -e["score"])
    ranked = [e for e in entries if not e["skip"]]
    skip = [e for e in entries if e["skip"]]
    top = ranked[:TOP_STORIES]

    summary = ""
    reason = ""
    whys: dict[int, str] = {}
    if top:
        try:
            result = await client.chat_json(
                settings.llm_long_model,
                prompts.DIGEST_SYSTEM,
                prompts.digest_user(
                    profile,
                    [
                        {
                            "ref": n,
                            "title": e["title"],
                            "sources": e["sources"],
                            "feed": e["feed"],
                            "tags": e["tags"],
                            "affinity": e["affinity"],
                        }
                        for n, e in enumerate(top, start=1)
                    ],
                    [e["title"] for e in skip],
                ),
                prompts.DIGEST_SCHEMA,
                max_tokens=900,
                workflow="digest",
                name="digest",
                reasoning=settings.llm_digest_reasoning,
            )
            summary = str(result["summary"])
            reason = str(result["safe_to_skip_reason"])
            whys = {int(s["cluster_ref"]): str(s["why"]) for s in result["top_stories"]}
        except LLMError as exc:
            log.warning("digest LLM call failed for %s: %s", user.id, exc)
            summary = f"{len(items)} new items across {len({i.feed_id for i in items})} sources."
            reason = "Low-affinity items from sources you rarely open."
    else:
        summary = "Nothing new in the last 24 hours."

    top_stories = [
        {
            "cluster_id": str(e["cluster_id"]) if e["cluster_id"] else None,
            "item_id": str(e["item_id"]),
            "title": e["title"],
            "why": whys.get(n, ""),
            "sources": e["sources"],
            "tag": e["tag"],
            "affinity": e["affinity"],
        }
        for n, e in enumerate(top, start=1)
    ]
    skip_item_ids = [str(i) for e in skip for i in e["member_ids"]]

    starred_unread = await _starred_unread(session, user.id)
    trends = await tag_trends(session, user.id)
    body = {
        "summary": summary,
        "top_stories": top_stories,
        "safe_to_skip": {
            "count": len(skip_item_ids),
            "story_count": len(skip),
            "item_ids": skip_item_ids,
            "reason": reason,
        },
        "starred_unread": starred_unread,
        "trends": trends,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "item_count": len(items),
    }
    refs = [uuid.UUID(s["item_id"]) for s in top_stories]
    return await _upsert_insight(session, user.id, "daily", period, f"Digest for {period}", body, refs)


async def _starred_unread(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    cutoff = utcnow() - timedelta(days=STARRED_UNREAD_DAYS)
    stmt = (
        select(models.Item.id, models.Item.title)
        .join(models.ItemState, models.ItemState.item_id == models.Item.id)
        .where(
            models.ItemState.user_id == user_id,
            models.ItemState.is_starred.is_(True),
            models.ItemState.is_read.is_(False),
            models.ItemState.starred_at <= cutoff,
        )
        .order_by(models.ItemState.starred_at)
        .limit(20)
    )
    return [{"item_id": str(i), "title": t} for i, t in (await session.execute(stmt)).all()]


# ---------------------------------------------------------------------------
# Weekly review
# ---------------------------------------------------------------------------


def week_bounds(iso_week: str) -> tuple[datetime, datetime]:
    year, week = iso_week.upper().split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    start = datetime.combine(monday, time(0, 0), tzinfo=_tz())
    return start, start + timedelta(days=7)


async def weekly_review(
    session: AsyncSession, user: models.User, iso_week: str, client: LLMClient | None = None
) -> models.Insight | None:
    if not ai_on(user, "digest"):
        return None
    client = client or get_client()
    settings = get_settings()
    start, end = week_bounds(iso_week)
    month_start = start - timedelta(days=30)

    feeds = {
        f.id: f
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    folders = {
        f.id: f.name
        for f in (await session.scalars(select(models.Folder).where(models.Folder.user_id == user.id))).all()
    }
    week_items = await _items_between(session, user.id, start, end)
    month_items = await _items_between(session, user.id, month_start, start)
    opened, skipped = await opened_and_skipped(session, user.id, month_start)
    ai_week = await _ai_rows(session, user.id, [i.id for i in week_items])
    ai_month = await _ai_rows(session, user.id, [i.id for i in month_items])

    volume_by_folder: Counter = Counter()
    most_read: Counter = Counter()
    per_feed_items: dict[uuid.UUID, list[ItemRef]] = defaultdict(list)
    week_tags: Counter = Counter()
    for it in week_items:
        feed = feeds.get(it.feed_id)
        folder = folders.get(feed.folder_id, "Inbox") if feed and feed.folder_id else "Inbox"
        volume_by_folder[folder] += 1
        per_feed_items[it.feed_id].append(it)
        if it.id in opened and feed:
            most_read[feed.title] += 1
        if it.id in ai_week:
            week_tags.update(ai_week[it.id].tags)
    month_tags: Counter = Counter()
    month_feed_tags: dict[uuid.UUID, Counter] = defaultdict(Counter)
    for it in month_items:
        if it.id in ai_month:
            month_tags.update(ai_month[it.id].tags)
            month_feed_tags[it.feed_id].update(ai_month[it.id].tags)
    weekly_avg = {t: c / 4.3 for t, c in month_tags.items()}
    changes = {t: week_tags.get(t, 0) - weekly_avg.get(t, 0.0) for t in set(week_tags) | set(month_tags)}
    rising = [
        {"name": t, "this_week": week_tags.get(t, 0), "delta": round(d, 1)}
        for t, d in sorted(changes.items(), key=lambda kv: -kv[1])
        if d > 0
    ][:5]
    fading = [
        {"name": t, "this_week": week_tags.get(t, 0), "delta": round(d, 1)}
        for t, d in sorted(changes.items(), key=lambda kv: kv[1])
        if d < 0
    ][:5]

    source_signals = []
    for fid, its in per_feed_items.items():
        feed = feeds.get(fid)
        if feed is None:
            continue
        n = len(its)
        week_feed_tags: Counter = Counter()
        for it in its:
            if it.id in ai_week:
                week_feed_tags.update(ai_week[it.id].tags)
        prior = month_feed_tags.get(fid, Counter())
        drift = round(1.0 - counter_cosine(week_feed_tags, prior), 3) if week_feed_tags and prior else None
        source_signals.append(
            {
                "feed_id": str(fid),
                "title": feed.title,
                "posts": n,
                "open_share": round(sum(1 for it in its if it.id in opened) / n, 3),
                "skip_share": round(sum(1 for it in its if it.id in skipped) / n, 3),
                "topic_drift": drift,
            }
        )
    source_signals.sort(key=lambda s: -s["posts"])

    stats = {
        "week": iso_week,
        "items": len(week_items),
        "opened": sum(1 for it in week_items if it.id in opened),
        "skipped": sum(1 for it in week_items if it.id in skipped),
        "volume_by_folder": dict(volume_by_folder.most_common()),
        "most_read_sources": dict(most_read.most_common(10)),
        "rising": rising,
        "fading": fading,
        "source_signals": source_signals[:25],
    }
    summary, highlights, suggestions = "", [], []
    try:
        result = await client.chat_json(
            settings.llm_long_model,
            prompts.WEEKLY_REVIEW_SYSTEM,
            prompts.weekly_review_user(await profile_text(session, user.id), stats),
            prompts.WEEKLY_REVIEW_SCHEMA,
            max_tokens=900,
            workflow="weekly_review",
            name="weekly_review",
            reasoning=settings.llm_digest_reasoning,
        )
        summary = str(result["summary"])
        highlights = [str(h) for h in result["highlights"]]
        suggestions = [str(s) for s in result["suggestions"]]
    except LLMError as exc:
        log.warning("weekly review LLM call failed for %s: %s", user.id, exc)
        summary = (
            f"{stats['items']} items this week; you opened {stats['opened']} and skipped {stats['skipped']}."
        )
    body = {"summary": summary, "highlights": highlights, "suggestions": suggestions, **stats}
    return await _upsert_insight(session, user.id, "weekly", iso_week, f"Week {iso_week} in review", body, [])


# ---------------------------------------------------------------------------
# Per-item summary
# ---------------------------------------------------------------------------


def render_summary(bullets: list[str], why: str) -> str:
    lines = [f"- {b.strip()}" for b in bullets if b and b.strip()]
    if not why or not why.strip():
        return "\n".join(lines) + "\n"
    return "\n".join(lines) + f"\n\n**Why this matters to you**\n\n{why.strip()}\n"


def summary_prefs(user: models.User | None) -> dict:
    """How summaries are written for this reader, from ``user.settings["summaries"]``, every key present:
    ``bullets`` (2/3/5), ``why`` (personal/general/off) and ``focus`` (free text the why-it-matters must weigh)."""
    raw = dict(((user.settings if user else None) or {}).get("summaries") or {})
    try:
        bullets = int(raw.get("bullets") or 3)
    except (TypeError, ValueError):
        bullets = 3
    if bullets not in prompts.SUMMARY_BULLET_CHOICES:
        bullets = 3
    why = str(raw.get("why") or "personal")
    return {
        "bullets": bullets,
        "why": why if why in prompts.WHY_MODES else "personal",
        "focus": " ".join(str(raw.get("focus") or "").split())[:SUMMARY_FOCUS_CHARS],
    }


def summary_prefs_from_form(form, current: dict) -> dict:
    out = dict(current)
    if form.get("bullets") is not None:
        out["bullets"] = form.get("bullets")
    if form.get("why") is not None:
        out["why"] = form.get("why")
    if form.get("focus") is not None:
        out["focus"] = form.get("focus")
    return summary_prefs(models.User(settings={"summaries": out}))


def _summary_entry_ok(entry: object, bullets: int = 3) -> bool:
    try:
        validate_schema(entry, prompts.item_summary_entry_schema(bullets))
    except LLMError as exc:
        log.warning("summarize_items: skipping malformed entry: %s", exc)
        return False
    return True


async def _story_siblings(
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """item_id -> cluster_id for the given items (only those in a cluster)."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(models.ClusterItem.item_id, models.ClusterItem.cluster_id)
            .join(models.Cluster, models.Cluster.id == models.ClusterItem.cluster_id)
            .where(models.Cluster.user_id == user_id, models.ClusterItem.item_id.in_(item_ids))
        )
    ).all()
    return {item_id: cluster_id for item_id, cluster_id in rows}


async def summarized_cluster_ids(session: AsyncSession, user_id: uuid.UUID, cluster_ids: set[uuid.UUID]) -> set:
    """Clusters that already have at least one member with a summary for this user."""
    if not cluster_ids:
        return set()
    stmt = (
        select(models.ClusterItem.cluster_id)
        .join(models.ItemAI, models.ItemAI.item_id == models.ClusterItem.item_id)
        .where(
            models.ClusterItem.cluster_id.in_(cluster_ids),
            models.ItemAI.user_id == user_id,
            models.ItemAI.summary.is_not(None),
        )
        .distinct()
    )
    return set((await session.scalars(stmt)).all())


async def items_without_summary(
    session: AsyncSession, user_id: uuid.UUID, since: datetime, *, until: datetime | None = None, limit: int = 500
) -> list[uuid.UUID]:
    """Ids of the user's items published in [since, until) that have no summary and whose story (cluster) has
    none either; newest first. What the sweep and the paper's "write missing summaries" hand to the job."""
    has_summary = (
        select(models.ItemAI.item_id)
        .where(
            models.ItemAI.item_id == models.Item.id,
            models.ItemAI.user_id == user_id,
            models.ItemAI.summary.is_not(None),
        )
        .exists()
    )
    stmt = (
        select(models.Item.id)
        .where(
            models.Item.feed_id.in_(user_feed_ids(user_id)),
            models.Item.published_at >= since,
            models.Item.published_at < (until or utcnow() + timedelta(days=1)),
            ~has_summary,
        )
        .order_by(models.Item.published_at.desc())
        .limit(limit * 4)
    )
    ids = list((await session.scalars(stmt)).all())
    if not ids:
        return []
    membership = await _story_siblings(session, user_id, ids)
    covered = await summarized_cluster_ids(session, user_id, set(membership.values()))
    out: list[uuid.UUID] = []
    seen_clusters: set[uuid.UUID] = set()
    for iid in ids:
        cid = membership.get(iid)
        if cid is not None:
            if cid in covered or cid in seen_clusters:
                continue
            seen_clusters.add(cid)
        out.append(iid)
        if len(out) >= limit:
            break
    return out


async def summarize_items(
    session: AsyncSession,
    user: models.User,
    items: list[models.Item],
    client: LLMClient | None = None,
    *,
    force: bool = False,
) -> dict[uuid.UUID, str]:
    """Eager summaries: 3 bullets + why-it-matters per item, in batches of ``SUMMARY_BATCH``, stored in
    ``item_ai.summary``. Returns {item_id: markdown} for what was written this call.

    One summary per *story*: items already summarised are skipped, and of the members of one cluster only
    one (the canonical item when present, else the newest) is summarised, unless ``force``. A batch that
    fails raises ``LLMError`` after the earlier batches were flushed; a malformed entry is skipped, not the
    batch.
    """
    if not items:
        return {}
    client = client or get_client()
    settings = get_settings()
    ids = [i.id for i in items]
    existing = await _ai_rows(session, user.id, ids)
    todo = items if force else [i for i in items if not (existing.get(i.id) and existing[i.id].summary)]
    if not force and todo:
        membership = await _story_siblings(session, user.id, [i.id for i in todo])
        done_clusters = await summarized_cluster_ids(session, user.id, set(membership.values()))
        canonical = {}
        if membership:
            rows = await session.execute(
                select(models.Cluster.id, models.Cluster.canonical_item_id).where(
                    models.Cluster.id.in_(set(membership.values()))
                )
            )
            canonical = dict(rows.all())
        picked: dict[uuid.UUID, models.Item] = {}
        singles: list[models.Item] = []
        for item in sorted(todo, key=lambda i: i.published_at, reverse=True):
            cid = membership.get(item.id)
            if cid is None:
                singles.append(item)
                continue
            if cid in done_clusters:
                continue
            best = picked.get(cid)
            if best is None or canonical.get(cid) == item.id:
                picked[cid] = item
        todo = singles + list(picked.values())
    if not todo:
        return {}
    feeds = {
        f.id: f.title
        for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()
    }
    profile = await profile_text(session, user.id)
    prefs = summary_prefs(user)
    written: dict[uuid.UUID, str] = {}

    async def run_batch(batch: list[models.Item]) -> list[models.Item]:
        """Summarise one batch; returns the items the model left out or mangled."""
        payload = [
            (n, it.title, feeds.get(it.feed_id, ""), (it.content_text or "")[:SUMMARY_BATCH_TEXT_CHARS])
            for n, it in enumerate(batch)
        ]
        result = await client.chat_json(
            settings.llm_fast_model,
            prompts.item_summary_batch_system(prefs["bullets"], prefs["why"]),
            prompts.item_summary_batch_user(profile, payload, focus=prefs["focus"]),
            prompts.item_summary_batch_schema(prefs["bullets"]),
            max_tokens=SUMMARY_TOKENS_PER_ITEM * len(batch) + SUMMARY_TOKENS_HEADROOM,
            workflow="summarize_items",
            name="item_summaries",
            validate_with=prompts.ITEM_SUMMARY_BATCH_LOOSE_SCHEMA,
        )
        by_index = {int(e["index"]): e for e in result["items"] if _summary_entry_ok(e, prefs["bullets"])}
        values = []
        missed: list[models.Item] = []
        for n, item in enumerate(batch):
            entry = by_index.get(n)
            if entry is None:
                missed.append(item)
                continue
            why = "" if prefs["why"] == "off" else str(entry["why_it_matters"])
            markdown = render_summary([str(b) for b in entry["bullets"]], why)
            written[item.id] = markdown
            values.append(
                {
                    "user_id": user.id,
                    "item_id": item.id,
                    "summary": markdown,
                    "model": settings.llm_fast_model,
                    "prompt_version": "",  # a summary-only row must not count as tagged (categorize.is_tagged)
                    "tags": [],
                    "confidences": {},
                    "generated_at": utcnow(),
                }
            )
        if values:
            # Upsert: a tagging job may create the row concurrently; on conflict only the summary changes.
            stmt = pg_insert(models.ItemAI).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "item_id"], set_={"summary": stmt.excluded.summary}
            )
            await session.execute(stmt)
            await session.flush()
        return missed

    missed: list[models.Item] = []
    for start in range(0, len(todo), SUMMARY_BATCH):
        missed += await run_batch(todo[start : start + SUMMARY_BATCH])
    # Retry once, in smaller batches, whatever the model dropped or mangled the first time round; anything
    # still missing is left for the sweep (``jobs.ai_summary_sweep``) or the reader's "Summarize now".
    if missed:
        log.info("summarize_items: retrying %d entries the model missed", len(missed))
        still = []
        for start in range(0, len(missed), SUMMARY_RETRY_BATCH):
            still += await run_batch(missed[start : start + SUMMARY_RETRY_BATCH])
        if still:
            log.warning("summarize_items: %d items still without a summary after retry", len(still))
    return written


async def summarize_item(
    session: AsyncSession,
    user: models.User,
    item: models.Item,
    client: LLMClient | None = None,
    *,
    hint: str = "",
) -> str:
    """Bullets + why-it-matters for one item, stored in ``item_ai.summary``. Raises LLMError on gateway failure.

    ``hint`` is the reader's note on a rejected summary (why the previous why-it-matters missed); the prompt
    carries it so the rewrite answers it.
    """
    client = client or get_client()
    settings = get_settings()
    profile = await profile_text(session, user.id)
    prefs = summary_prefs(user)
    result = await client.chat_json(
        settings.llm_fast_model,
        prompts.item_summary_system(prefs["bullets"], prefs["why"]),
        prompts.item_summary_user(
            profile,
            item.title,
            (item.content_text or "")[:SUMMARY_TEXT_CHARS],
            focus=prefs["focus"],
            hint=" ".join(hint.split())[:SUMMARY_FOCUS_CHARS],
        ),
        prompts.item_summary_schema(prefs["bullets"]),
        max_tokens=SUMMARY_TOKENS_PER_ITEM * 2 + SUMMARY_TOKENS_HEADROOM,
        workflow="summarize_item",
        name="item_summary",
    )
    why = "" if prefs["why"] == "off" else str(result["why_it_matters"])
    markdown = render_summary([str(b) for b in result["bullets"]], why)
    row = await session.scalar(
        select(models.ItemAI).where(models.ItemAI.user_id == user.id, models.ItemAI.item_id == item.id)
    )
    if row is None:
        row = models.ItemAI(
            user_id=user.id,
            item_id=item.id,
            model=settings.llm_fast_model,
            prompt_version=prompts.PROMPT_VERSION,
        )
        session.add(row)
    row.summary = markdown
    await session.flush()
    return markdown
