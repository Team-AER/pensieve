"""Fever API tests."""

from __future__ import annotations

import base64

from sqlalchemy import select

from pensieve import models
from pensieve.syncapi.common import long_id
from pensieve.syncapi.fever import fever_api_key_hash
from tests.test_syncapi_helpers import FEVER_SECRET, make_second_user, seed, state_of

FEVER = "/api/fever/"


async def _call(client, key: str | None, *flags: str, **params) -> dict:
    query = "&".join(["api", *flags])
    data = {("as" if k == "as_" else k): str(v) for k, v in params.items()}
    if key is not None:
        data["api_key"] = key
    r = await client.post(f"{FEVER}?{query}", data=data)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    return r.json()


def _ids(csv: str) -> set[int]:
    return {int(x) for x in csv.split(",") if x}


async def test_auth_hash_scheme_and_responses(client, session, user):
    d = await seed(session, user)
    assert d.fever_key == fever_api_key_hash(user.email, FEVER_SECRET)
    assert len(d.fever_key) == 32

    assert await _call(client, None) == {"api_version": 3, "auth": 0}
    assert await _call(client, "0" * 32) == {"api_version": 3, "auth": 0}
    body = await _call(client, d.fever_key)
    assert body["api_version"] == 3 and body["auth"] == 1
    assert body["last_refreshed_on_time"] == int(d.feed_a.last_success_at.timestamp())

    # GET with the same params, and the alternate mount points.
    for path in ("/fever/", "/fever", "/api/fever"):
        r = await client.get(path, params={"api": "", "api_key": d.fever_key})
        assert r.status_code == 200 and r.json()["auth"] == 1, path

    # A greader token is not a Fever key.
    assert (await _call(client, d.token))["auth"] == 0

    token = await session.scalar(
        select(models.ApiToken)
        .where(models.ApiToken.token_hash == d.fever_key)
        .execution_options(populate_existing=True)
    )
    assert token.last_used_at is not None


async def test_groups_feeds_and_favicons(client, session, user):
    d = await seed(session, user)
    body = await _call(client, d.fever_key, "groups", "feeds", "favicons")
    groups = {g["id"]: g["title"] for g in body["groups"]}
    assert groups == {long_id(d.tech.id): "Tech", long_id(d.news.id): "News"}
    fg = {g["group_id"]: _ids(g["feed_ids"]) for g in body["feeds_groups"]}
    assert fg[long_id(d.tech.id)] == {long_id(d.feed_a.id)}
    assert fg[long_id(d.news.id)] == {long_id(d.feed_b.id)}

    feeds = {f["id"]: f for f in body["feeds"]}
    assert set(feeds) == {long_id(d.feed_a.id), long_id(d.feed_b.id), long_id(d.feed_c.id)}
    a = feeds[long_id(d.feed_a.id)]
    assert a["title"] == "Feed A" and a["url"] == d.feed_a.url and a["site_url"] == "https://a.example.com"
    assert a["is_spark"] == 0 and a["favicon_id"] == 0  # no cached icon bytes yet
    assert a["last_updated_on_time"] == int(d.feed_a.last_success_at.timestamp())
    assert body["favicons"] == []

    d.feed_a.icon_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    d.feed_a.icon_content_type = "image/png"
    await session.commit()
    body = await _call(client, d.fever_key, "feeds", "favicons")
    a = next(f for f in body["feeds"] if f["id"] == long_id(d.feed_a.id))
    assert a["favicon_id"] == long_id(d.feed_a.id)
    expected = "image/png;base64," + base64.b64encode(d.feed_a.icon_data).decode()
    assert body["favicons"] == [{"id": long_id(d.feed_a.id), "data": expected}]


async def test_items_paging_since_id_max_id_with_ids(client, session, user):
    d = await seed(session, user)
    visible = {long_id(i.id) for i in d.visible_newest_first}

    body = await _call(client, d.fever_key, "items")
    assert body["total_items"] == 5
    default_ids = [i["id"] for i in body["items"]]
    assert set(default_ids) == visible and default_ids == sorted(default_ids, reverse=True)

    item = next(i for i in body["items"] if i["id"] == long_id(d.c1.id))
    assert item == {
        "id": long_id(d.c1.id),
        "feed_id": long_id(d.feed_c.id),
        "title": "Item c1",
        "author": "Ann Author",
        "html": "<p>Body of c1</p>",
        "url": d.c1.url,
        "is_saved": 1,
        "is_read": 1,
        "created_on_time": int(d.c1.published_at.timestamp()),
    }

    # since_id walks upward in id order; hidden b1 never appears.
    body = await _call(client, d.fever_key, "items", since_id=0)
    asc = [i["id"] for i in body["items"]]
    assert asc == sorted(visible)
    body = await _call(client, d.fever_key, "items", since_id=asc[2])
    assert [i["id"] for i in body["items"]] == asc[3:]
    body = await _call(client, d.fever_key, "items", since_id=asc[-1])
    assert body["items"] == []

    body = await _call(client, d.fever_key, "items", max_id=asc[2])
    assert [i["id"] for i in body["items"]] == [asc[1], asc[0]]

    body = await _call(
        client, d.fever_key, "items", with_ids=f"{long_id(d.a1.id)},{long_id(d.b1.id)},{long_id(d.c1.id)}"
    )
    assert {i["id"] for i in body["items"]} == {long_id(d.a1.id), long_id(d.c1.id)}


async def test_items_page_size_is_50(client, session, user):
    d = await seed(session, user)
    from tests.test_syncapi_helpers import _item

    session.add_all([_item(d.feed_c, f"x{n}", 10 + n) for n in range(60)])
    await session.commit()
    body = await _call(client, d.fever_key, "items", since_id=0)
    assert len(body["items"]) == 50 and body["total_items"] == 65
    body2 = await _call(client, d.fever_key, "items", since_id=body["items"][-1]["id"])
    assert len(body2["items"]) == 15


async def test_unread_and_saved_item_ids(client, session, user):
    d = await seed(session, user)
    body = await _call(client, d.fever_key, "unread_item_ids", "saved_item_ids")
    assert _ids(body["unread_item_ids"]) == {long_id(d.a1.id), long_id(d.a3.id), long_id(d.b2.id)}
    assert _ids(body["saved_item_ids"]) == {long_id(d.a3.id), long_id(d.c1.id)}


async def test_mark_item_feed_group(client, session, user):
    d = await seed(session, user)
    key = d.fever_key

    body = await _call(client, key, "unread_item_ids", mark="item", as_="read", id=long_id(d.a1.id))
    assert body["auth"] == 1
    assert (await state_of(session, user, d.a1)).is_read
    await _call(client, key, mark="item", as_="unread", id=long_id(d.a1.id))
    assert not (await state_of(session, user, d.a1)).is_read
    await _call(client, key, mark="item", as_="saved", id=long_id(d.a1.id))
    assert (await state_of(session, user, d.a1)).is_starred
    await _call(client, key, mark="item", as_="unsaved", id=long_id(d.a1.id))
    assert not (await state_of(session, user, d.a1)).is_starred

    # Whole feed, with `before` (seconds) excluding newer items.
    before = int(d.a2.published_at.timestamp())
    await _call(client, key, mark="feed", as_="read", id=long_id(d.feed_a.id), before=before)
    assert (await state_of(session, user, d.a1)).is_read
    assert not (await state_of(session, user, d.a3)).is_read
    await _call(client, key, mark="feed", as_="read", id=long_id(d.feed_a.id))
    assert (await state_of(session, user, d.a3)).is_read

    # Group = folder; hidden items stay untouched.
    await _call(client, key, mark="group", as_="read", id=long_id(d.news.id))
    assert (await state_of(session, user, d.b2)).is_read
    assert not (await state_of(session, user, d.b1)).is_read

    body = await _call(client, key, "unread_item_ids")
    assert body["unread_item_ids"] == ""


async def test_mark_group_zero_means_everything(client, session, user):
    d = await seed(session, user)
    await _call(client, d.fever_key, mark="group", as_="read", id=0)
    body = await _call(client, d.fever_key, "unread_item_ids")
    assert body["unread_item_ids"] == ""


async def test_fever_user_scoping(client, session, user):
    d = await seed(session, user)
    _other, _, other_key = await make_second_user(session)
    body = await _call(client, other_key, "groups", "feeds", "items", "unread_item_ids", "saved_item_ids")
    assert body["groups"] == [] and [f["title"] for f in body["feeds"]] == ["O"]
    assert [i["title"] for i in body["items"]] == ["Item o1"] and body["total_items"] == 1
    assert body["saved_item_ids"] == ""
    await _call(client, other_key, mark="item", as_="read", id=long_id(d.a1.id))
    assert await state_of(session, user, d.a1) is None
    await _call(client, other_key, mark="feed", as_="read", id=long_id(d.feed_a.id))
    assert (
        await state_of(session, user, d.a3) is not None and not (await state_of(session, user, d.a3)).is_read
    )
