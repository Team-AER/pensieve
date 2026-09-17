"""Grouped reader list: the representative of a cluster is the canonical item only while the view still shows
it; otherwise the newest member that passes the filter (unread member after the canonical was read, a feed's
own member in that feed's view)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pensieve import models
from tests.test_web_support import HX, login, seed_feed, seed_item


async def seed_cluster(session, user):
    f1 = await seed_feed(session, user, "Outlet A")
    f2 = await seed_feed(session, user, "Outlet B")
    now = datetime.now(UTC)
    a = await seed_item(session, f1, "Big launch announced", published_at=now - timedelta(hours=3))
    b = await seed_item(session, f2, "Big launch: what we know", published_at=now - timedelta(hours=2))
    c = await seed_item(session, f2, "Big launch: the details", published_at=now - timedelta(hours=1))
    solo = await seed_item(session, f1, "Unrelated post", published_at=now - timedelta(hours=4))
    cluster = models.Cluster(
        user_id=user.id,
        headline="Big launch",
        window_start=a.published_at,
        window_end=c.published_at,
        canonical_item_id=a.id,
        source_count=2,
    )
    session.add(cluster)
    await session.flush()
    session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=i.id) for i in (a, b, c)])
    await session.commit()
    return f1, f2, a, b, c, solo, cluster


async def test_canonical_shown_while_visible_then_newest_unread_member(client, session, user):
    _f1, _f2, a, b, c, solo, _cluster = await seed_cluster(session, user)
    await login(client, user)
    r = await client.get("/reader/unread/list?grouped=1", headers=HX)
    assert r.status_code == 200
    assert a.title in r.text and b.title not in r.text and c.title not in r.text and solo.title in r.text
    # reading the canonical must not hide the story from the unread view: the newest unread member stands in
    session.add(models.ItemState(user_id=user.id, item_id=a.id, is_read=True, read_at=datetime.now(UTC)))
    await session.commit()
    r = await client.get("/reader/unread/list?grouped=1", headers=HX)
    assert a.title not in r.text and c.title in r.text and b.title not in r.text and "sources" in r.text
    # the all-items view still leads with the canonical
    r = await client.get("/reader/all/list?grouped=1", headers=HX)
    assert a.title in r.text and b.title not in r.text and c.title not in r.text


async def test_feed_view_shows_own_member_of_cross_feed_story(client, session, user):
    f1, f2, a, b, c, solo, _cluster = await seed_cluster(session, user)
    await login(client, user)
    r = await client.get(f"/reader/feed/{f2.id}/list?grouped=1", headers=HX)
    assert r.status_code == 200
    assert c.title in r.text and b.title not in r.text and a.title not in r.text and solo.title not in r.text
    r = await client.get(f"/reader/feed/{f1.id}/list?grouped=1", headers=HX)
    assert a.title in r.text and solo.title in r.text and c.title not in r.text
    # ungrouped: every member of the feed
    r = await client.get(f"/reader/feed/{f2.id}/list?grouped=0", headers=HX)
    assert b.title in r.text and c.title in r.text
