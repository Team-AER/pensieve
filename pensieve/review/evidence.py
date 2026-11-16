"""Measured exact overlap and estimated story overlap, using existing item history."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from sqlalchemy import select

from pensieve.ai.cluster import load_overrides
from pensieve.ai.common import is_batch_read, mark_all_batch_times
from pensieve.models import Cluster, ClusterItem, Feed, Item, ItemState, Note, SubscriptionEvent
from pensieve.subscriptions import followed


@dataclass
class Evidence:
    feed: Feed
    start: datetime
    end: datetime
    items: list = field(default_factory=list)
    stories: int = 0
    exact: int = 0
    shared: int = 0
    available_shared: int = 0
    first: int = 0
    only_here: list = field(default_factory=list)
    opened_only_here: list = field(default_factory=list)
    starred_only_here: list = field(default_factory=list)
    noted_only_here: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    followed_since: datetime | None = None
    healthy: bool = False
    recently_kept: bool = False
    paper_limited: bool = False

    @property
    def size(self):
        return len(self.items)

    @property
    def per_day(self):
        return round(self.stories / max(1, (self.end - self.start).days), 2)

    @property
    def exact_share(self):
        return self.exact / self.size if self.size else 0

    @property
    def repeated_share(self):
        return self.shared / self.size if self.size else 0

    def signals(self):
        return {
            "feed_id": str(self.feed.id),
            "title": self.feed.title,
            "sample_size": self.size,
            "window_start": self.start.isoformat(),
            "window_end": self.end.isoformat(),
            "stories_per_day": self.per_day,
            "exact_copies_measured": self.exact,
            "shared_stories_estimated": self.shared,
            "shared_with_currently_healthy_sources": self.available_shared,
            "published_first": self.first,
            "only_here": len(self.only_here),
            "opened_only_here": len(self.opened_only_here),
            "starred_only_here": len(self.starred_only_here),
            "noted_only_here": len(self.noted_only_here),
            "healthy_evidence": self.healthy,
        }


async def evidence(session, user, *, days=30, now=None):
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    feeds = list(
        await session.scalars(
            select(Feed)
            .where(Feed.user_id == user.id, followed(), Feed.kind == "rss")
            .execution_options(populate_existing=True)
        )
    )
    by_feed = {f.id: Evidence(f, start, now, followed_since=f.created_at) for f in feeds}
    if not feeds:
        return []
    rows = (
        await session.execute(
            select(Item, ItemState)
            .execution_options(populate_existing=True)
            .outerjoin(ItemState, (ItemState.item_id == Item.id) & (ItemState.user_id == user.id))
            .where(Item.feed_id.in_(by_feed), Item.published_at >= start, Item.published_at <= now)
            .order_by(Item.published_at)
        )
    ).all()
    items = {it.id: it for it, _ in rows}
    states = {it.id: st for it, st in rows if st}
    noted = set(
        await session.scalars(select(Note.item_id).where(Note.user_id == user.id, Note.item_id.in_(items)))
    )
    batches = await mark_all_batch_times(session, user.id, start)
    overrides = await load_overrides(session, user.id)
    hashes, urls, clusters = defaultdict(set), defaultdict(set), defaultdict(set)
    groups = defaultdict(set)
    for it in items.values():
        by_feed[it.feed_id].items.append(it)
        if it.hash:
            hashes[it.hash].add(it.id)
        if it.url:
            urls[it.url].add(it.id)
    for cid, iid in await session.execute(
        select(ClusterItem.cluster_id, ClusterItem.item_id)
        .join(Cluster, Cluster.id == ClusterItem.cluster_id)
        .where(
            Cluster.user_id == user.id,
            Cluster.kind.in_(["story", "duplicate"]),
            ClusterItem.item_id.in_(items),
        )
    ):
        clusters[cid].add(iid)
        groups[iid].add(cid)
    manual = defaultdict(set)
    for pair, action in overrides.items():
        if action == "merge" and pair <= items.keys():
            a, b = tuple(pair)
            manual[a].add(b)
            manual[b].add(a)
    for it in items.values():
        ev = by_feed[it.feed_id]
        exact_ids = (hashes[it.hash] if it.hash else set()) | (urls[it.url] if it.url else set())
        shared_ids = exact_ids | manual[it.id] | set().union(*(clusters[cid] for cid in groups[it.id]))

        def permitted(ids, own=it):
            return {
                iid
                for iid in ids
                if items[iid].feed_id != own.feed_id and overrides.get(frozenset((own.id, iid))) != "split"
            }

        exact_ids, shared_ids = permitted(exact_ids), permitted(shared_ids)
        ev.exact += bool(exact_ids)
        ev.shared += bool(shared_ids)
        ev.available_shared += any(
            not by_feed[items[iid].feed_id].feed.paused
            and by_feed[items[iid].feed_id].feed.error_count == 0
            and by_feed[items[iid].feed_id].feed.last_success_at
            and by_feed[items[iid].feed_id].feed.last_success_at >= now - timedelta(days=2)
            for iid in shared_ids
        )
        if shared_ids:
            ev.first += all(it.published_at < items[iid].published_at for iid in shared_ids)
            if len(ev.examples) < 5:
                other = min((items[iid] for iid in shared_ids), key=lambda i: i.published_at)
                ev.examples.append((it, other))
        else:
            ev.only_here.append(it)
            st = states.get(it.id)
            if st and st.is_read and st.read_at and not is_batch_read(st, batches):
                ev.opened_only_here.append(it)
            if st and st.is_starred:
                ev.starred_only_here.append(it)
            if it.id in noted:
                ev.noted_only_here.append(it)
    events = list(
        await session.scalars(
            select(SubscriptionEvent)
            .where(SubscriptionEvent.user_id == user.id)
            .order_by(SubscriptionEvent.created_at)
        )
    )
    for ev in by_feed.values():
        history = [e for e in events if e.feed_id == ev.feed.id]
        for event in history:
            if event.action in {"follow", "restore"}:
                ev.followed_since = event.created_at
        undone = {event.source.removeprefix("undo:") for event in history if event.action == "undo"}
        ev.recently_kept = any(
            e.action == "keep" and str(e.id) not in undone and e.created_at >= now - timedelta(days=90)
            for e in history
        )
        ev.paper_limited = bool(
            (user.settings or {}).get("paper", {}).get("feed_limits", {}).get(str(ev.feed.id))
        )
        # Distinct acquisitions across the window avoid treating an initial import or outage backfill
        # as a healthy fetch history. Absence of that evidence withholds recommendations.
        acquired_days = {it.fetched_at.date() for it in ev.items if start <= it.fetched_at <= now}
        published_days = {it.published_at.date() for it in ev.items}
        timely = sum(abs((it.fetched_at - it.published_at).total_seconds()) <= 2 * 86400 for it in ev.items)
        acquisition_dates = sorted(it.fetched_at for it in ev.items if start <= it.fetched_at <= now)
        observed_start = max(start, ev.followed_since)
        acquisition_gaps = [b - a for a, b in pairwise(acquisition_dates)]
        continuous = bool(
            acquisition_dates
            and acquisition_dates[0] <= observed_start + timedelta(days=3)
            and all(gap <= timedelta(days=3) for gap in acquisition_gaps)
        )
        ev.healthy = bool(
            continuous
            and not ev.feed.paused
            and ev.feed.error_count == 0
            and ev.feed.last_success_at
            and ev.feed.last_success_at >= now - timedelta(days=2)
            and len(acquired_days) >= 14
            and len(published_days) >= 14
            and timely >= ev.size * 0.9
            and not any(e.action == "pause" and e.created_at >= start for e in history)
        )
        # Count stories within the feed using the same exact and story relationships as its evidence.
        parent = {it.id: it.id for it in ev.items}

        def root(iid, parent=parent):
            while parent[iid] != iid:
                parent[iid] = parent[parent[iid]]
                iid = parent[iid]
            return iid

        for item in ev.items:
            related = (hashes[item.hash] if item.hash else set()) | (urls[item.url] if item.url else set())
            related |= set().union(*(clusters[cid] for cid in groups[item.id]))
            for iid in related & parent.keys():
                if overrides.get(frozenset((item.id, iid))) != "split":
                    parent[root(iid)] = root(item.id)
        ev.stories = len({root(iid) for iid in parent})
        for key in ("only_here", "opened_only_here", "starred_only_here", "noted_only_here"):
            values = getattr(ev, key)
            setattr(ev, key, list({root(item.id): item for item in values}.values()))
    return list(by_feed.values())


async def preview_evidence(session, user, sample, now=None):
    """Preview counterpart of the same URL/hash evidence, with title similarity replacing stored clusters."""
    from pensieve.ai.cluster import jaccard, title_tokens

    now = now or datetime.now(UTC)
    start = now - timedelta(days=14)
    rows = list(
        await session.scalars(
            select(Item)
            .join(Feed, Feed.id == Item.feed_id)
            .where(
                Feed.user_id == user.id,
                followed(),
                Feed.kind == "rss",
                Item.published_at >= start,
                Item.published_at <= now,
            )
        )
    )
    urls = {it.url for it in rows if it.url}
    hashes = {it.hash for it in rows if it.hash}
    titles = [title_tokens(it.title) for it in rows]
    exact, estimated = 0, 0
    entries = sample.get("entries", [])
    for entry in entries:
        matches = bool(
            (entry.get("url") and entry["url"] in urls) or (entry.get("hash") and entry["hash"] in hashes)
        )
        exact += matches
        tokens = title_tokens(entry.get("title", ""))
        estimated += matches or (len(tokens) >= 4 and any(jaccard(tokens, other) >= 0.5 for other in titles))
    return {
        "sample_size": len(entries),
        "reader_sample_size": len(rows),
        "exact": exact,
        "estimated": estimated,
        "potentially_added": len(entries) - estimated,
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
    }
