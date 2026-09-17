"""Web hardening and reader-parity: keyset paging, nav-count cache, favicons route, undo cap, manage
routes (resume all, interval, retroactive rules, dismiss suggestion, font settings), secure cookie setting."""

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pensieve import models
from pensieve.config import get_settings
from pensieve.web import queries
from pensieve.web.cursor import decode_cursor, encode_cursor
from pensieve.web.favicons import letter_avatar_svg
from pensieve.web.reader import PAGE_SIZE, View, nav_data
from pensieve.web.templating import make_csrf
from tests.test_web_support import HX, fake_module, login, memory_limiter, seed_feed, seed_item  # noqa: F401


def _cursor_from(html: str) -> str | None:
    m = re.search(r"&after=([^\"&]+)\"", html)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Keyset pagination
# ---------------------------------------------------------------------------


async def test_list_pages_by_cursor_and_is_stable_under_inserts(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    base = datetime(2026, 9, 1, tzinfo=UTC)
    titles = []
    for i in range(PAGE_SIZE + 5):
        titles.append(f"Story {i:03d}")
        await seed_item(session, feed, titles[-1], published_at=base + timedelta(minutes=i))
    await login(client, user)

    r = await client.get("/reader/all/list?grouped=0", headers=HX)
    assert r.status_code == 200
    page1 = re.findall(r'class="item-title">(Story \d+)<', r.text)
    assert len(page1) == PAGE_SIZE and page1[0] == titles[-1]
    cursor = _cursor_from(r.text)
    assert cursor and "page=" not in r.text
    decoded = decode_cursor(cursor)
    assert decoded is not None

    # A newer item arrives between the two requests: page 2 must not repeat or skip anything.
    await seed_item(session, feed, "Breaking", published_at=base + timedelta(days=1))
    r = await client.get(f"/reader/all/list?grouped=0&after={cursor}", headers=HX)
    page2 = re.findall(r'class="item-title">(Story \d+|Breaking)<', r.text)
    assert page2 == titles[:5][::-1]
    assert "Breaking" not in page2 and _cursor_from(r.text) is None
    assert "list-empty" not in r.text and "<html" not in r.text

    # Oldest-first uses the same cursor with the opposite comparison.
    r = await client.get("/reader/all/list?grouped=0&sort=oldest", headers=HX)
    first = re.findall(r'class="item-title">(Story \d+)<', r.text)
    assert first[0] == titles[0]
    r = await client.get(f"/reader/all/list?grouped=0&sort=oldest&after={_cursor_from(r.text)}", headers=HX)
    rest = re.findall(r'class="item-title">(Story \d+|Breaking)<', r.text)
    assert rest == [*titles[PAGE_SIZE:], "Breaking"]

    # A garbage cursor falls back to the first page instead of erroring.
    r = await client.get("/reader/all/list?grouped=0&after=nonsense", headers=HX)
    assert r.status_code == 200 and titles[-1] in r.text


def test_cursor_roundtrip():
    item = models.Item(
        feed_id=None, guid="g", title="t", published_at=datetime(2026, 9, 18, 1, 2, 3, 456789, tzinfo=UTC), hash="h"
    )
    item.id = models._uuid7()
    assert decode_cursor(encode_cursor(item)) == (item.published_at, item.id)
    assert decode_cursor("") is None and decode_cursor("x.y") is None


# ---------------------------------------------------------------------------
# Nav count cache
# ---------------------------------------------------------------------------


async def test_nav_counts_are_cached_and_invalidated_on_state_writes(client, session, user, monkeypatch):
    feed = await seed_feed(session, user, "Feed")
    a = await seed_item(session, feed, "A")
    b = await seed_item(session, feed, "B")
    view = View("unread", title="All unread")
    nav = await nav_data(session, user, view)
    assert nav.total_unread == 2
    # A direct DB write without invalidation is not visible inside the TTL...
    session.add(models.ItemState(user_id=user.id, item_id=a.id, is_read=True))
    await session.commit()
    assert (await nav_data(session, user, view)).total_unread == 2
    # ...but is after invalidation, and the web write path invalidates for us.
    queries.invalidate_nav_cache(user.id)
    assert (await nav_data(session, user, view)).total_unread == 1
    await queries.set_read(session, user.id, [b.id], True)
    await session.commit()
    assert (await nav_data(session, user, view)).total_unread == 0
    # The TTL expires entries on its own.
    state = await session.get(models.ItemState, (user.id, a.id))
    state.is_read = False
    await session.commit()
    assert (await nav_data(session, user, view)).total_unread == 0
    monkeypatch.setattr(queries, "NAV_CACHE_TTL_S", 0.0)
    assert (await nav_data(session, user, view)).total_unread == 1


async def test_nav_cache_is_per_user(client, session, user):
    from tests.test_web_support import make_user

    other = await make_user(session)
    mine = await seed_feed(session, user, "Mine")
    await seed_item(session, mine, "x")
    theirs = await seed_feed(session, other, "Theirs")
    await seed_item(session, theirs, "y")
    await seed_item(session, theirs, "z")
    assert (await nav_data(session, user, View("unread"))).total_unread == 1
    assert (await nav_data(session, other, View("unread"))).total_unread == 2


# ---------------------------------------------------------------------------
# Favicons
# ---------------------------------------------------------------------------


async def test_favicon_route_serves_bytes_or_letter_avatar(client, session, user):
    from tests.test_web_support import make_user

    feed = await seed_feed(session, user, "Quiet Signal")
    await login(client, user)
    r = await client.get(f"/favicons/{feed.id}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert ">Q<" in r.text and "max-age=604800" in r.headers["cache-control"]
    feed.icon_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    feed.icon_content_type = "image/png"
    await session.commit()
    r = await client.get(f"/favicons/{feed.id}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == bytes(feed.icon_data)
    etag = r.headers["etag"]
    r = await client.get(f"/favicons/{feed.id}", headers={"If-None-Match": etag})
    assert r.status_code == 304
    # Another user's feed icon is not reachable.
    other = await make_user(session)
    theirs = await seed_feed(session, other, "Theirs")
    r = await client.get(f"/favicons/{theirs.id}")
    assert r.status_code == 404
    # The nav uses the route for every feed.
    r = await client.get("/")
    assert f'src="/favicons/{feed.id}"' in r.text


def test_letter_avatar_is_escaped_and_stable():
    svg = letter_avatar_svg("<b>bad", "seed")
    assert "&lt;" not in svg and ">B<" in svg
    assert letter_avatar_svg("x", "same") == letter_avatar_svg("x", "same")
    assert ">#<" in letter_avatar_svg("", "s")


# ---------------------------------------------------------------------------
# Mark all read: server-side write, capped undo via token
# ---------------------------------------------------------------------------


async def test_mark_all_read_undo_is_capped_and_token_is_single_use(client, session, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "undo_batch_max_ids", 3)
    feed = await seed_feed(session, user, "Feed")
    items = [await seed_item(session, feed, f"S{i}") for i in range(5)]
    headers = await login(client, user)
    r = await client.post("/reader/unread/mark-read", data={"older_than": ""}, headers=headers | HX)
    assert r.status_code == 200 and "Marked 5 items as read" in r.text and "Undo last 3" in r.text
    token = re.search(r'name="token" value="([^"]*)"', r.text).group(1)
    rows = list(await session.scalars(select(models.ItemState).where(models.ItemState.user_id == user.id)))
    assert len(rows) == 5 and all(s.is_read and s.read_at for s in rows)
    r = await client.post("/items/undo-read", data={"token": token, "view": "unread"}, headers=headers | HX)
    assert r.status_code == 200
    unread = await session.scalar(
        select(models.ItemState).where(models.ItemState.user_id == user.id, models.ItemState.is_read.is_(False))
    )
    assert unread is not None
    count_unread = len(
        list(await session.scalars(select(models.ItemState).where(models.ItemState.user_id == user.id, models.ItemState.is_read.is_(False))))
    )
    assert count_unread == 3
    # Replaying the token does nothing more; a forged ids list is capped too.
    r = await client.post("/items/undo-read", data={"token": token, "view": "unread"}, headers=headers | HX)
    assert r.status_code == 200
    assert len(list(await session.scalars(select(models.ItemState).where(models.ItemState.user_id == user.id, models.ItemState.is_read.is_(False))))) == 3
    ids = ",".join(str(i.id) for i in items)
    await client.post("/items/undo-read", data={"ids": ids, "view": "unread"}, headers=headers | HX)
    assert len(list(await session.scalars(select(models.ItemState).where(models.ItemState.user_id == user.id, models.ItemState.is_read.is_(False))))) == 3


# ---------------------------------------------------------------------------
# Manage: feed health, resume all, interval, rules retroactively, dismiss, font
# ---------------------------------------------------------------------------


async def test_feed_health_and_resume_all_and_interval(client, session, user):
    now = datetime.now(UTC)
    a = await seed_feed(session, user, "Paused A", paused=True, error_count=7, last_error="HTTP 500", last_fetch_at=now - timedelta(hours=2), next_fetch_at=now + timedelta(hours=3))
    b = await seed_feed(session, user, "Paused B", paused=True, error_count=10)
    c = await seed_feed(session, user, "Healthy C", last_fetch_at=now - timedelta(minutes=5), next_fetch_at=now + timedelta(minutes=10), last_success_at=now)
    headers = await login(client, user)
    r = await client.get("/manage/feeds")
    assert r.status_code == 200
    assert "Resume all paused (2)" in r.text
    assert "7 consecutive errors" in r.text and "not scheduled" in r.text
    assert "last 5m" in r.text and "next " in r.text and 'name="fetch_interval_min"' in r.text
    r = await client.post("/manage/feeds/resume-all", headers=headers)
    assert r.status_code == 303 and r.headers["location"].endswith("feeds_resumed")
    for f in (a, b):
        await session.refresh(f)
        assert not f.paused and f.error_count == 0 and f.next_fetch_at <= datetime.now(UTC)
    r = await client.get("/manage/feeds")
    assert "Resume all paused" not in r.text

    s = get_settings()
    r = await client.post(f"/manage/feeds/{c.id}/interval", data={"fetch_interval_min": "45"}, headers=headers)
    assert r.status_code == 303
    await session.refresh(c)
    assert c.fetch_interval_min == 45 and c.next_fetch_at <= c.last_fetch_at + timedelta(minutes=45)
    await client.post(f"/manage/feeds/{c.id}/interval", data={"fetch_interval_min": "1"}, headers=headers)
    await session.refresh(c)
    assert c.fetch_interval_min == s.fetch_min_interval_min
    await client.post(f"/manage/feeds/{c.id}/interval", data={"fetch_interval_min": "999999"}, headers=headers)
    await session.refresh(c)
    assert c.fetch_interval_min == s.fetch_max_interval_min
    r = await client.post(f"/manage/feeds/{c.id}/interval", data={"fetch_interval_min": "soon"}, headers=headers)
    assert "err=" in r.headers["location"]


async def test_move_feed_over_htmx_returns_204_and_refreshes_counts(client, session, user):
    folder = models.Folder(user_id=user.id, name="Target")
    session.add(folder)
    await session.commit()
    feed = await seed_feed(session, user, "Draggable")
    headers = await login(client, user)
    r = await client.get("/")
    assert f'data-feed-id="{feed.id}"' in r.text and f'data-drop-folder="{folder.id}"' in r.text
    r = await client.post(f"/manage/feeds/{feed.id}/move", data={"folder_id": str(folder.id)}, headers=headers | HX)
    assert r.status_code == 204 and "counts-changed" in r.headers["HX-Trigger"]
    await session.refresh(feed)
    assert feed.folder_id == folder.id
    r = await client.post(f"/manage/feeds/{feed.id}/move", data={"folder_id": ""}, headers=headers | HX)
    await session.refresh(feed)
    assert r.status_code == 204 and feed.folder_id is None


async def test_rule_applies_to_existing_items(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    other = await seed_feed(session, user, "Other")
    hit = await seed_item(session, feed, "Sponsored: buy now")
    miss = await seed_item(session, feed, "Real story")
    elsewhere = await seed_item(session, other, "Sponsored elsewhere")
    starred = await seed_item(session, feed, "Sponsored but starred")
    session.add(models.ItemState(user_id=user.id, item_id=starred.id, is_starred=True, tags=["keep"]))
    rule = models.FeedRule(user_id=user.id, feed_id=feed.id, field="title", pattern="sponsored", action="hide")
    session.add(rule)
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/manage/rules")
    assert f"/manage/rules/{rule.id}/apply" in r.text
    r = await client.post(f"/manage/rules/{rule.id}/apply", headers=headers)
    assert r.status_code == 303 and r.headers["location"].endswith("rule_applied")
    assert (await session.get(models.ItemState, (user.id, hit.id))).hidden
    assert await session.get(models.ItemState, (user.id, miss.id)) is None
    assert await session.get(models.ItemState, (user.id, elsewhere.id)) is None  # scoped to the rule's feed
    merged = await session.get(models.ItemState, (user.id, starred.id))
    await session.refresh(merged)
    assert merged.hidden and merged.is_starred and merged.tags == ["keep"]  # merged, not overwritten
    # A global tag rule reaches every feed and appends tags.
    tag_rule = models.FeedRule(user_id=user.id, feed_id=None, field="title", pattern="sponsored", action="tag", action_value="ads")
    session.add(tag_rule)
    await session.commit()
    await client.post(f"/manage/rules/{tag_rule.id}/apply", headers=headers)
    await session.refresh(merged)
    assert merged.tags == ["keep", "ads"]
    assert (await session.get(models.ItemState, (user.id, elsewhere.id))).tags == ["ads"]


async def test_dismiss_suggestion_calls_ai_service_and_tolerates_absence(client, session, user, monkeypatch):
    suggested = models.Folder(user_id=user.id, name="Suggested")
    session.add(suggested)
    await session.commit()
    feed = await seed_feed(session, user, "Inbox feed", suggested_folder_id=suggested.id, suggested_folder_confidence=0.7)
    headers = await login(client, user)
    r = await client.get("/manage/feeds?folder=inbox")
    assert f"/manage/feeds/{feed.id}/dismiss-suggestion" in r.text and ">Dismiss<" in r.text

    called = []

    async def dismiss_folder_suggestion(session_, user_, feed_):
        called.append(feed_.id)
        feed_.suggested_folder_id = None

    fake_module(monkeypatch, "pensieve.ai.service", dismiss_folder_suggestion=dismiss_folder_suggestion)
    r = await client.post(f"/manage/feeds/{feed.id}/dismiss-suggestion", headers=headers)
    assert r.status_code == 303 and r.headers["location"].endswith("suggestion_dismissed") and called == [feed.id]
    await session.refresh(feed)
    assert feed.suggested_folder_id is None and feed.folder_id is None

    # Without the ai package (or without the function) the route still clears the suggestion.
    feed.suggested_folder_id = suggested.id
    await session.commit()
    fake_module(monkeypatch, "pensieve.ai.service")
    r = await client.post(f"/manage/feeds/{feed.id}/dismiss-suggestion", headers=headers | HX)
    assert r.status_code == 204
    await session.refresh(feed)
    assert feed.suggested_folder_id is None


async def test_font_size_and_measure_settings(client, session, user):
    headers = await login(client, user)
    r = await client.get("/")
    assert 'data-font="m"' in r.text and 'data-measure="normal"' in r.text
    r = await client.post("/manage/account/profile", data={"theme": "auto", "font_size": "xl", "measure": "wide"}, headers=headers)
    assert r.status_code == 303
    await session.refresh(user)
    assert user.settings["font_size"] == "xl" and user.settings["measure"] == "wide"
    r = await client.get("/manage/account")
    assert 'data-font="xl"' in r.text and 'data-measure="wide"' in r.text and 'value="xl" selected' in r.text
    # The +/- keys post here.
    r = await client.post("/manage/account/font", data={"font_size": "s"}, headers=headers)
    assert r.status_code == 204
    await session.refresh(user)
    assert user.settings["font_size"] == "s" and user.settings["measure"] == "wide"
    r = await client.post("/manage/account/font", data={"font_size": "huge"}, headers=headers)
    assert r.status_code == 204
    await session.refresh(user)
    assert user.settings["font_size"] == "s"


async def test_toolbar_share_and_shortcuts_listed(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Share me", url="https://example.com/share")
    await login(client, user)
    r = await client.get(f"/items/{item.id}", headers=HX)
    assert 'data-action="share"' in r.text and 'data-share-url="https://example.com/share"' in r.text
    r = await client.get("/")
    assert "Mark everything in this view" in r.text and "Larger / smaller reading text" in r.text


async def test_session_cookie_secure_follows_setting(client, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "session_cookie_secure", True)
    r = await client.post("/login", data={"email": user.email, "password": "password123", "csrf_token": make_csrf(None)})
    assert r.status_code == 303
    assert "secure" in r.headers["set-cookie"].lower()
    monkeypatch.setattr(get_settings(), "session_cookie_secure", False)
    await client.post("/logout", headers={"X-CSRF-Token": make_csrf(user.id)})
    r = await client.post("/login", data={"email": user.email, "password": "password123", "csrf_token": make_csrf(None)})
    assert "secure" not in r.headers["set-cookie"].lower()


async def test_hsts_only_when_secure(user, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from pensieve.main import create_app

    monkeypatch.setattr(get_settings(), "session_cookie_secure", True)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert r.headers["content-security-policy"]
