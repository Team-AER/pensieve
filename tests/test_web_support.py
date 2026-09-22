"""Helpers shared by the web package tests (no tests in here)."""

from __future__ import annotations

import sys
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.auth import hash_password
from pensieve.web import ratelimit
from pensieve.web.templating import make_csrf

HTML = {"Accept": "text/html"}
HX = {"HX-Request": "true"}


def login_form(email: str, password: str, **extra: str) -> dict[str, str]:
    """A /login form body including the anonymous CSRF token the login page embeds."""
    return {"email": email, "password": password, "csrf_token": make_csrf(None), **extra}


async def login(client: AsyncClient, user: models.User, password: str = "password123") -> dict[str, str]:
    """Sign in through /login and return headers carrying the CSRF token for POSTs."""
    r = await client.post("/login", data=login_form(user.email, password))
    assert r.status_code == 303, r.text
    assert "pensieve_session" in r.cookies
    return {"X-CSRF-Token": make_csrf(user.id)}


@pytest.fixture(autouse=True)
def memory_limiter():
    """Never touch Redis for sign-in rate limiting in tests; a fresh in-memory limiter per test."""
    limiter = ratelimit.MemoryRateLimiter()
    ratelimit.set_limiter(limiter)
    yield limiter
    ratelimit.set_limiter(None)


async def make_user(session: AsyncSession, role: models.UserRole = models.UserRole.reader) -> models.User:
    u = models.User(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Other",
        password_hash=hash_password("password123"),
        role=role,
    )
    session.add(u)
    await session.commit()
    return u


async def seed_feed(
    session: AsyncSession,
    user: models.User,
    title: str = "Example",
    folder: models.Folder | None = None,
    **kw,
) -> models.Feed:
    feed = models.Feed(
        user_id=user.id,
        url=f"https://{uuid.uuid4().hex[:8]}.example.com/feed.xml",
        title=title,
        folder_id=folder.id if folder else None,
        **kw,
    )
    session.add(feed)
    await session.commit()
    return feed


async def seed_item(
    session: AsyncSession,
    feed: models.Feed,
    title: str,
    text: str = "",
    published_at: datetime | None = None,
    url: str | None = None,
) -> models.Item:
    item = models.Item(
        feed_id=feed.id,
        guid=uuid.uuid4().hex,
        url=url or f"https://example.com/{uuid.uuid4().hex[:6]}",
        title=title,
        published_at=published_at or datetime.now(UTC) - timedelta(hours=1),
        content_html=f"<p>{text or title}</p>",
        content_text=text or title,
        hash=uuid.uuid4().hex,
    )
    session.add(item)
    await session.commit()
    return item


def fake_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    """Install a stand-in for a sibling package module so lazy imports resolve to test doubles."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod
