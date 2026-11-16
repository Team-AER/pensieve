"""Local discovery signals. No crawling, network probing, or semantic analysis."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from lxml import html
from sqlalchemy import or_, select

from pensieve.ai.common import is_batch_read, mark_all_batch_times
from pensieve.models import Feed, Folder, Item, ItemAI, ItemState
from pensieve.streams import load_catalog
from pensieve.subscriptions import followed


def domain(url):
    try:
        return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


async def cited_domains(session, user, now=None):
    start = (now or datetime.now(UTC)) - timedelta(days=60)
    feeds = list(await session.scalars(select(Feed).where(Feed.user_id == user.id, followed())))
    excluded = {domain(url) for feed in feeds for url in (feed.url, feed.site_url)}
    rows = (
        await session.execute(
            select(Item, ItemState)
            .join(ItemState, Item.id == ItemState.item_id)
            .join(Feed, Feed.id == Item.feed_id)
            .where(
                Feed.user_id == user.id,
                ItemState.user_id == user.id,
                or_(ItemState.read_at >= start, ItemState.starred_at >= start),
            )
        )
    ).all()
    batches = await mark_all_batch_times(session, user.id, start)
    counts = Counter()
    for item, state in rows:
        if not (
            (state.is_starred and state.starred_at and state.starred_at >= start)
            or (
                state.is_read
                and state.read_at
                and state.read_at >= start
                and not is_batch_read(state, batches)
            )
        ):
            continue
        try:
            doc = html.fromstring(item.content_html or "<div/>")
        except (ValueError, html.etree.ParserError):
            continue
        links = set()
        for href in doc.xpath("//a/@href"):
            url = urljoin(item.url or "", href)
            name = domain(url)
            if (
                urlsplit(url).scheme in {"http", "https"}
                and name
                and name not in excluded
                and name != domain(item.url)
            ):
                links.add(name)
        counts.update(links)
    return [
        {"domain": name, "count": count, "url": "https://" + name}
        for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        if count >= 3
    ]


async def explore_topics(session, user, *, item_id=None, folder_id=None):
    catalog = load_catalog()
    known = {source.url: stream.topic for stream in catalog.streams for source in stream.sources}
    topics, words = set(), set()
    if item_id:
        row = (
            await session.execute(
                select(Item, Feed)
                .join(Feed, Item.feed_id == Feed.id)
                .where(Item.id == item_id, Feed.user_id == user.id)
            )
        ).first()
        if row is None:
            return None
        item, feed = row
        if feed.url in known:
            topics.add(known[feed.url])
        words.update(item.title.lower().split())
        ai = await session.get(ItemAI, (user.id, item.id))
        if ai:
            words.update(t.lower() for t in ai.tags)
    elif folder_id:
        folder = await session.get(Folder, folder_id)
        if folder is None or folder.user_id != user.id:
            return None
        words.update(folder.name.lower().split())
        feeds = await session.scalars(
            select(Feed).where(Feed.user_id == user.id, followed(), Feed.folder_id == folder.id)
        )
        topics.update(known[f.url] for f in feeds if f.url in known)
    topic_words = {
        "science": {"science", "space", "research", "nasa", "physics", "biology"},
        "technology": {"technology", "software", "python", "web", "engineering", "programming"},
    }
    for topic in {stream.topic for stream in catalog.streams}:
        if topic in words or words & topic_words.get(topic, set()):
            topics.add(topic)
    return topics


async def source_suggestions(session, user, topics=None):
    following = set(await session.scalars(select(Feed.url).where(Feed.user_id == user.id, followed())))
    dismissed = set((user.settings or {}).get("discovery_dismissed", []))
    rows = {}
    for stream in load_catalog().streams:
        if topics is not None and stream.topic not in topics:
            continue
        for source in stream.sources:
            if source.url in following or source.url in dismissed:
                continue
            rows.setdefault(
                source.url,
                {
                    "source": source,
                    "topic": stream.topic,
                    "why": f"Shares the {stream.topic} topic"
                    if topics is not None
                    else f"From the {stream.title} curated stream",
                },
            )
    return [rows[url] for url in sorted(rows)]
