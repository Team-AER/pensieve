
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pensieve import models
from tests.test_web_support import HX, login, make_user, memory_limiter, seed_feed, seed_item  # noqa: F401


async def test_reader_lists_items_with_unread_counts(client, session, user):
    folder = models.Folder(user_id=user.id, name="Tech")
    session.add(folder)
    await session.commit()
    feed = await seed_feed(session, user, "Example Blog", folder=folder)
    for i in range(3):
        await seed_item(session, feed, f"Story number {i}")
    headers = await login(client, user)
    r = await client.get("/")
    assert r.status_code == 200
    assert "Story number 0" in r.text and "Story number 2" in r.text
    assert 'class="count">3</span>' in r.text  # All unread and the Tech folder both show 3
    assert "3 unread · 3 total" in r.text
    r = await client.get(f"/reader/folder/{folder.id}")
    assert r.status_code == 200 and "Tech" in r.text
    r = await client.get(f"/reader/feed/{feed.id}")
    assert r.status_code == 200 and "Example Blog" in r.text
    # HTMX partial returns only the list pane.
    r = await client.get("/reader/unread/list", headers=HX)
    assert r.status_code == 200 and "<html" not in r.text and "Story number 1" in r.text
    r = await client.get("/reader/nav?view=unread", headers=HX)
    assert r.status_code == 200 and "Tech" in r.text
    assert headers


async def test_reader_scopes_by_user(client, session, user):
    mine = await seed_feed(session, user, "Mine")
    await seed_item(session, mine, "My private story")
    other = await make_user(session)
    theirs = await seed_feed(session, other, "Theirs")
    their_item = await seed_item(session, theirs, "Their secret story")
    await login(client, user)
    r = await client.get("/reader/all")
    assert "My private story" in r.text
    assert "Their secret story" not in r.text
    assert 'class="count">1</span>' in r.text
    r = await client.get(f"/items/{their_item.id}", headers=HX)
    assert r.status_code == 404
    r = await client.get(f"/reader/feed/{theirs.id}")
    assert r.status_code == 404


async def test_grouped_view_one_row_per_cluster(client, session, user):
    f1 = await seed_feed(session, user, "Outlet A")
    f2 = await seed_feed(session, user, "Outlet B")
    a = await seed_item(session, f1, "Big launch announced")
    b = await seed_item(session, f2, "Big launch: what we know")
    solo = await seed_item(session, f1, "Unrelated post")
    now = datetime.now(UTC)
    cluster = models.Cluster(
        user_id=user.id,
        headline="Big launch",
        window_start=now - timedelta(days=1),
        window_end=now,
        canonical_item_id=a.id,
        source_count=2,
    )
    session.add(cluster)
    await session.flush()
    session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=a.id), models.ClusterItem(cluster_id=cluster.id, item_id=b.id)])
    await session.commit()
    await login(client, user)
    r = await client.get("/reader/all/list?grouped=1", headers=HX)
    assert r.status_code == 200
    assert "Big launch announced" in r.text and "Unrelated post" in r.text
    assert "Big launch: what we know" not in r.text
    assert "2 sources" in r.text
    r = await client.get("/reader/all/list?grouped=0", headers=HX)
    assert "Big launch: what we know" in r.text and "sources" not in r.text
    r = await client.get(f"/clusters/{cluster.id}", headers=HX)
    assert r.status_code == 200 and "Outlet A" in r.text and "Outlet B" in r.text and solo.title not in r.text


async def test_mark_all_read_with_undo(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    old = await seed_item(session, feed, "Old story", published_at=datetime.now(UTC) - timedelta(days=3))
    new = await seed_item(session, feed, "Fresh story")
    headers = await login(client, user)
    # Older than a day: only the old one.
    r = await client.post("/reader/unread/mark-read", data={"older_than": "1d"}, headers=headers | HX)
    assert r.status_code == 200
    assert "Marked 1 item as read" in r.text
    token = re.search(r'name="token" value="([^"]*)"', r.text).group(1)
    assert token and str(old.id) not in r.text.split("undo-bar")[1].split("</form>")[0]  # only the token travels
    state = await session.get(models.ItemState, (user.id, old.id))
    assert state is not None and state.is_read
    assert await session.get(models.ItemState, (user.id, new.id)) is None
    # Undo restores (and a token is single-use).
    r = await client.post("/items/undo-read", data={"token": token, "view": "unread"}, headers=headers | HX)
    assert r.status_code == 200
    await session.refresh(state)
    assert not state.is_read
    # Everything.
    r = await client.post("/reader/unread/mark-read", data={"older_than": ""}, headers=headers | HX)
    assert "Marked 2 items as read" in r.text
    session.expire(state)
    rows = list(await session.scalars(select(models.ItemState).where(models.ItemState.user_id == user.id)))
    assert all(s.is_read for s in rows) and len(rows) == 2
    r = await client.get("/reader/unread/list", headers=HX)
    assert "0 unread · 2 total" in r.text and "All caught up" in r.text


async def test_tag_view_and_sorting(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    first = await seed_item(session, feed, "First", published_at=datetime.now(UTC) - timedelta(days=2))
    second = await seed_item(session, feed, "Second", published_at=datetime.now(UTC) - timedelta(days=1))
    session.add(models.ItemAI(user_id=user.id, item_id=first.id, tags=["databases"], confidences={"databases": 0.9}))
    session.add(models.ItemState(user_id=user.id, item_id=second.id, tags=["todo"]))
    await session.commit()
    await login(client, user)
    r = await client.get("/reader/tag/databases")
    assert r.status_code == 200 and "First" in r.text and ">Second<" not in r.text
    r = await client.get("/reader/tag/todo/list", headers=HX)
    assert "Second" in r.text and ">First<" not in r.text
    r = await client.get("/reader/all/list?sort=oldest", headers=HX)
    assert r.text.index("First") < r.text.index("Second")
    r = await client.get("/reader/all/list?sort=newest", headers=HX)
    assert r.text.index("Second") < r.text.index("First")


async def test_hidden_items_excluded(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    shown = await seed_item(session, feed, "Visible one")
    hidden = await seed_item(session, feed, "Hidden one")
    session.add(models.ItemState(user_id=user.id, item_id=hidden.id, hidden=True))
    await session.commit()
    await login(client, user)
    r = await client.get("/")
    assert shown.title in r.text and hidden.title not in r.text
    assert 'class="count">1</span>' in r.text


async def test_search_finds_by_title(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    hit = await seed_item(session, feed, "Postgres upgrade notes", text="Moving to Postgres 17 with pgvector.")
    await seed_item(session, feed, "Cooking with cast iron")
    other = await make_user(session)
    ofeed = await seed_feed(session, other, "Other")
    await seed_item(session, ofeed, "Postgres secrets of another user")
    await login(client, user)
    r = await client.get("/search?q=postgres")
    assert r.status_code == 200
    assert hit.title in r.text and "Cooking" not in r.text and "another user" not in r.text
    assert "1 result for" in r.text
    r = await client.get("/search?q=postgres&state=starred", headers=HX)
    assert "No matches" in r.text
    r = await client.get("/search?q=zzzzqqq", headers=HX)
    assert "No matches" in r.text
