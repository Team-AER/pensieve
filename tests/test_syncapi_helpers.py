"""Seed data and helpers shared by the syncapi tests (no tests in this module)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.auth import generate_api_token, hash_api_token
from pensieve.syncapi.fever import fever_api_key_hash

BASE_TIME = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
FEVER_SECRET = "s3cret-fever"


@dataclass
class Seed:
    user: models.User
    token: str
    fever_key: str
    tech: models.Folder
    news: models.Folder
    feed_a: models.Feed
    feed_b: models.Feed
    feed_c: models.Feed
    a1: models.Item
    a2: models.Item
    a3: models.Item
    b1: models.Item
    b2: models.Item
    c1: models.Item
    items: dict[str, models.Item] = field(default_factory=dict)

    @property
    def visible_newest_first(self) -> list[models.Item]:
        return [self.c1, self.b2, self.a3, self.a2, self.a1]


def _item(feed: models.Feed, key: str, hours: int, **extra) -> models.Item:
    return models.Item(
        feed_id=feed.id,
        guid=f"{feed.url}#{key}",
        url=f"{feed.site_url}/{key}",
        title=f"Item {key}",
        author=extra.pop("author", "Ann Author"),
        published_at=BASE_TIME + timedelta(hours=hours),
        content_html=f"<p>Body of {key}</p>",
        content_text=f"Body of {key}",
        hash=uuid.uuid4().hex,
        **extra,
    )


async def seed(session: AsyncSession, user: models.User) -> Seed:
    """Two folders, three feeds, six items in mixed states.

    Newest first (visible): c1, b2, a3, a2, a1. b1 is hidden.
    a1 unread, AI-tagged 'rust'; a2 read; a3 starred+unread; b1 hidden unread; b2 unread tagged 'later';
    c1 read+starred (feed c has no folder).
    """
    tech = models.Folder(user_id=user.id, name="Tech", position=1)
    news = models.Folder(user_id=user.id, name="News", position=2)
    session.add_all([tech, news])
    await session.flush()

    feed_a = models.Feed(
        user_id=user.id,
        folder_id=tech.id,
        url="https://a.example.com/rss",
        site_url="https://a.example.com",
        title="Feed A",
        icon_url="https://a.example.com/icon.png",
        position=1,
        last_success_at=BASE_TIME + timedelta(hours=7),
    )
    feed_b = models.Feed(
        user_id=user.id,
        folder_id=news.id,
        url="https://b.example.com/rss",
        site_url="https://b.example.com",
        title="Feed B",
        position=2,
    )
    feed_c = models.Feed(
        user_id=user.id,
        url="https://c.example.com/rss",
        site_url="https://c.example.com",
        title="Feed C",
        position=3,
    )
    session.add_all([feed_a, feed_b, feed_c])
    await session.flush()

    a1 = _item(feed_a, "a1", 1)
    a2 = _item(feed_a, "a2", 2)
    a3 = _item(feed_a, "a3", 3, enclosure_url="https://a.example.com/a3.mp3", enclosure_type="audio/mpeg")
    b1 = _item(feed_b, "b1", 4)
    b2 = _item(feed_b, "b2", 5)
    c1 = _item(feed_c, "c1", 6)
    session.add_all([a1, a2, a3, b1, b2, c1])
    await session.flush()

    now = datetime.now(UTC)
    session.add_all(
        [
            models.ItemState(user_id=user.id, item_id=a2.id, is_read=True, read_at=now),
            models.ItemState(user_id=user.id, item_id=a3.id, is_starred=True, starred_at=now),
            models.ItemState(user_id=user.id, item_id=b1.id, hidden=True),
            models.ItemState(user_id=user.id, item_id=b2.id, tags=["later"]),
            models.ItemState(
                user_id=user.id, item_id=c1.id, is_read=True, is_starred=True, read_at=now, starred_at=now
            ),
            models.Tag(user_id=user.id, name="rust", kind="ai"),
            models.Tag(user_id=user.id, name="python", kind="ai"),
            models.ItemAI(user_id=user.id, item_id=a1.id, tags=["rust"], confidences={"rust": 0.9}),
        ]
    )

    token = generate_api_token()
    session.add(
        models.ApiToken(user_id=user.id, label="Reeder", token_hash=hash_api_token(token), kind="greader")
    )
    fever_key = fever_api_key_hash(user.email, FEVER_SECRET)
    session.add(models.ApiToken(user_id=user.id, label="Unread", token_hash=fever_key, kind="fever"))
    await session.commit()

    return Seed(
        user=user,
        token=token,
        fever_key=fever_key,
        tech=tech,
        news=news,
        feed_a=feed_a,
        feed_b=feed_b,
        feed_c=feed_c,
        a1=a1,
        a2=a2,
        a3=a3,
        b1=b1,
        b2=b2,
        c1=c1,
        items={"a1": a1, "a2": a2, "a3": a3, "b1": b1, "b2": b2, "c1": c1},
    )


async def make_second_user(session: AsyncSession) -> tuple[models.User, str, str]:
    """A second user with one feed/item and both token kinds; returns (user, greader token, fever key)."""
    from pensieve.auth import hash_password

    other = models.User(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Other",
        password_hash=hash_password("pw"),
    )
    session.add(other)
    await session.flush()
    feed = models.Feed(
        user_id=other.id, url="https://o.example.com/rss", site_url="https://o.example.com", title="O"
    )
    session.add(feed)
    await session.flush()
    session.add(_item(feed, "o1", 9))
    token = generate_api_token()
    session.add(
        models.ApiToken(user_id=other.id, label="t", token_hash=hash_api_token(token), kind="greader")
    )
    fever_key = fever_api_key_hash(other.email, "other-secret")
    session.add(models.ApiToken(user_id=other.id, label="f", token_hash=fever_key, kind="fever"))
    await session.commit()
    return other, token, fever_key


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"GoogleLogin auth={token}"}


async def state_of(session: AsyncSession, user: models.User, item: models.Item) -> models.ItemState | None:
    """Fresh read of an item's state (bypasses identity-map caching)."""
    return await session.scalar(
        select(models.ItemState)
        .where(models.ItemState.user_id == user.id, models.ItemState.item_id == item.id)
        .execution_options(populate_existing=True)
    )


async def fresh_feed(session: AsyncSession, feed_id: uuid.UUID) -> models.Feed | None:
    return await session.scalar(
        select(models.Feed).where(models.Feed.id == feed_id).execution_options(populate_existing=True)
    )
