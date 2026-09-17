"""Google Reader API tests (Reeder / NetNewsWire / Unread dialect)."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from sqlalchemy import select

from pensieve import models
from pensieve.syncapi.common import (
    ITEM_TAG_PREFIX,
    item_hex_id,
    item_long_id,
    item_tag_id,
    long_id_sql,
    parse_item_id,
    parse_stream,
    to_usec,
)
from tests.test_syncapi_helpers import auth, fresh_feed, make_second_user, seed, state_of

API = "/reader/api/0"
READING_LIST = "user/-/state/com.google/reading-list"
STARRED = "user/-/state/com.google/starred"
READ = "user/-/state/com.google/read"
KEPT_UNREAD = "user/-/state/com.google/kept-unread"


def _titles(payload: dict) -> list[str]:
    return [i["title"] for i in payload["items"]]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_client_login_with_token(client, session, user):
    d = await seed(session, user)
    r = await client.post("/accounts/ClientLogin", data={"Email": user.email, "Passwd": d.token})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert f"Auth={d.token}\n" in r.text and r.text.startswith("SID=")

    # Legacy clients also GET with query params, and the prefixed mount works too.
    r = await client.get("/api/greader/accounts/ClientLogin", params={"Email": user.email, "Passwd": d.token})
    assert r.status_code == 200 and f"Auth={d.token}" in r.text

    # A valid token presented with someone else's email is rejected.
    r = await client.post("/accounts/ClientLogin", data={"Email": "nobody@example.com", "Passwd": d.token})
    assert r.status_code == 401 and "BadAuthentication" in r.text


async def test_client_login_with_password_mints_token(client, session, user):
    r = await client.post("/accounts/ClientLogin", data={"Email": user.email, "Passwd": "password123"})
    assert r.status_code == 200
    token = next(line.split("=", 1)[1] for line in r.text.splitlines() if line.startswith("Auth="))
    assert token and token != "password123"

    row = await session.scalar(select(models.ApiToken).where(models.ApiToken.user_id == user.id))
    assert row is not None and row.kind == "greader" and row.label == "Google Reader client"

    r = await client.get(f"{API}/user-info", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["userEmail"] == user.email and r.json()["userId"] == str(user.id)

    r = await client.post("/accounts/ClientLogin", data={"Email": user.email, "Passwd": "wrong"})
    assert r.status_code == 401 and r.text.startswith("Error=BadAuthentication")


async def test_bad_token_is_401_with_header(client, session, user):
    await seed(session, user)
    r = await client.get(f"{API}/subscription/list", headers=auth("nope"))
    assert r.status_code == 401
    assert r.headers.get("x-reader-google-bad-token") == "true"
    r = await client.get(f"{API}/subscription/list")
    assert r.status_code == 401 and r.headers.get("x-reader-google-bad-token") == "true"
    r = await client.post(f"{API}/edit-tag", data={"i": "1", "a": READ})
    assert r.status_code == 401


async def test_token_userinfo_preferences_and_methods(client, session, user):
    d = await seed(session, user)
    r = await client.get(f"{API}/token", headers=auth(d.token))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and len(r.text) > 20

    r = await client.get(f"{API}/user-info", headers=auth(d.token), params={"output": "json"})
    assert set(r.json()) == {"userId", "userName", "userProfileId", "userEmail"}

    r = await client.get(f"{API}/preference/list", headers=auth(d.token))
    assert r.json()["prefs"][0]["id"] == "lhn-prefs"
    r = await client.get(f"{API}/preference/stream/list", headers=auth(d.token))
    assert r.json() == {"streamprefs": {}}

    assert (await client.options(f"{API}/edit-tag")).status_code < 500
    assert (await client.head(f"{API}/user-info", headers=auth(d.token))).status_code < 500


# ---------------------------------------------------------------------------
# Subscriptions and tags
# ---------------------------------------------------------------------------


async def test_subscription_list_with_folders(client, session, user):
    d = await seed(session, user)
    for prefix in ("", "/api/greader"):
        r = await client.get(
            f"{prefix}{API}/subscription/list", headers=auth(d.token), params={"output": "json"}
        )
        assert r.status_code == 200, prefix
        subs = {s["id"]: s for s in r.json()["subscriptions"]}
        assert set(subs) == {f"feed/{d.feed_a.id}", f"feed/{d.feed_b.id}", f"feed/{d.feed_c.id}"}
        a = subs[f"feed/{d.feed_a.id}"]
        assert a["title"] == "Feed A" and a["url"] == d.feed_a.url and a["htmlUrl"] == "https://a.example.com"
        assert a["iconUrl"] == "https://a.example.com/icon.png"
        assert a["categories"] == [{"id": "user/-/label/Tech", "label": "Tech"}]
        assert subs[f"feed/{d.feed_b.id}"]["categories"] == [{"id": "user/-/label/News", "label": "News"}]
        assert subs[f"feed/{d.feed_c.id}"]["categories"] == []


async def test_tag_list_includes_folders_user_tags_and_ai_tags(client, session, user):
    d = await seed(session, user)
    r = await client.get(f"{API}/tag/list", headers=auth(d.token), params={"output": "json"})
    tags = {t["id"]: t for t in r.json()["tags"]}
    assert STARRED in tags
    assert tags["user/-/label/Tech"]["type"] == "folder"
    assert tags["user/-/label/News"]["type"] == "folder"
    assert tags["user/-/label/later"]["type"] == "tag"
    assert tags["user/-/label/AI: rust"]["type"] == "tag"
    assert "user/-/label/AI: python" not in tags  # in the vocabulary but on no item


async def test_unread_count_with_mixed_states_and_hidden(client, session, user):
    d = await seed(session, user)
    r = await client.get(f"{API}/unread-count", headers=auth(d.token), params={"output": "json"})
    body = r.json()
    assert body["max"] == 1000
    counts = {c["id"]: c for c in body["unreadcounts"]}
    assert counts[f"feed/{d.feed_a.id}"]["count"] == 2  # a1, a3 (a2 read)
    assert counts[f"feed/{d.feed_b.id}"]["count"] == 1  # b2 (b1 hidden)
    assert f"feed/{d.feed_c.id}" not in counts  # c1 read
    assert counts["user/-/label/Tech"]["count"] == 2
    assert counts["user/-/label/News"]["count"] == 1
    assert counts[READING_LIST]["count"] == 3
    assert counts[READING_LIST]["newestItemTimestampUsec"] == str(to_usec(d.b2.published_at))
    assert counts[f"feed/{d.feed_a.id}"]["newestItemTimestampUsec"] == str(to_usec(d.a3.published_at))


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


async def test_stream_contents_reading_list_paging_and_shape(client, session, user):
    d = await seed(session, user)
    url = f"{API}/stream/contents/{quote(READING_LIST, safe='')}"
    r = await client.get(url, headers=auth(d.token), params={"n": 2, "output": "json"})
    body = r.json()
    assert body["id"] == READING_LIST and isinstance(body["updated"], int)
    assert _titles(body) == ["Item c1", "Item b2"] and body["continuation"]

    r = await client.get(url, headers=auth(d.token), params={"n": 2, "c": body["continuation"]})
    body2 = r.json()
    assert _titles(body2) == ["Item a3", "Item a2"] and body2["continuation"]

    r = await client.get(url, headers=auth(d.token), params={"n": 2, "c": body2["continuation"]})
    body3 = r.json()
    assert _titles(body3) == ["Item a1"] and "continuation" not in body3

    # Item shape as Reeder expects it.
    c1 = body["items"][0]
    assert c1["id"] == item_tag_id(d.c1.id)
    assert c1["timestampUsec"] == str(to_usec(d.c1.published_at))
    assert c1["published"] == int(d.c1.published_at.timestamp())
    assert c1["canonical"] == [{"href": d.c1.url}]
    assert c1["alternate"] == [{"href": d.c1.url, "type": "text/html"}]
    assert c1["origin"] == {
        "streamId": f"feed/{d.feed_c.id}",
        "title": "Feed C",
        "htmlUrl": "https://c.example.com",
    }
    assert c1["summary"]["content"] == "<p>Body of c1</p>"
    assert c1["author"] == "Ann Author"
    assert READING_LIST in c1["categories"] and READ in c1["categories"] and STARRED in c1["categories"]
    assert not any(cat.startswith("user/-/label/") for cat in c1["categories"])  # feed c has no folder

    a3 = body2["items"][0]
    assert a3["enclosure"] == [{"href": "https://a.example.com/a3.mp3", "type": "audio/mpeg"}]
    assert (
        "user/-/label/Tech" in a3["categories"]
        and STARRED in a3["categories"]
        and READ not in a3["categories"]
    )

    # Unencoded stream id in the path and the bare /stream/contents form both work.
    r = await client.get(f"{API}/stream/contents/{READING_LIST}", headers=auth(d.token), params={"n": 10})
    assert len(r.json()["items"]) == 5
    r = await client.get(f"{API}/stream/contents", headers=auth(d.token), params={"n": 10})
    assert len(r.json()["items"]) == 5


async def test_stream_contents_filters(client, session, user):
    d = await seed(session, user)
    rl = f"{API}/stream/contents/{quote(READING_LIST, safe='')}"

    r = await client.get(rl, headers=auth(d.token), params={"n": 50, "xt": READ})
    assert _titles(r.json()) == ["Item b2", "Item a3", "Item a1"]

    r = await client.get(rl, headers=auth(d.token), params={"n": 50, "r": "o"})
    assert _titles(r.json()) == ["Item a1", "Item a2", "Item a3", "Item b2", "Item c1"]

    ot = int(d.a3.published_at.timestamp())
    nt = int(d.b2.published_at.timestamp())
    r = await client.get(rl, headers=auth(d.token), params={"n": 50, "ot": ot, "nt": nt})
    assert _titles(r.json()) == ["Item b2", "Item a3"]

    r = await client.get(f"{API}/stream/contents/{quote(STARRED, safe='')}", headers=auth(d.token))
    assert _titles(r.json()) == ["Item c1", "Item a3"]

    r = await client.get(f"{API}/stream/contents/feed/{d.feed_a.id}", headers=auth(d.token))
    assert _titles(r.json()) == ["Item a3", "Item a2", "Item a1"]
    assert r.json()["id"] == f"feed/{d.feed_a.id}"

    r = await client.get(
        f"{API}/stream/contents/{quote('user/-/label/Tech', safe='')}", headers=auth(d.token)
    )
    assert _titles(r.json()) == ["Item a3", "Item a2", "Item a1"]
    r = await client.get(
        f"{API}/stream/contents/user/-/label/Tech", headers=auth(d.token), params={"xt": READ}
    )
    assert _titles(r.json()) == ["Item a3", "Item a1"]

    r = await client.get(f"{API}/stream/contents/user/-/label/later", headers=auth(d.token))
    assert _titles(r.json()) == ["Item b2"]
    assert "user/-/label/later" in r.json()["items"][0]["categories"]

    r = await client.get(
        f"{API}/stream/contents/{quote('user/-/label/AI: rust', safe='')}", headers=auth(d.token)
    )
    assert _titles(r.json()) == ["Item a1"]
    assert "user/-/label/AI: rust" in r.json()["items"][0]["categories"]

    r = await client.get(f"{API}/stream/contents/user/-/label/Nope", headers=auth(d.token))
    assert r.json()["items"] == []
    r = await client.get(f"{API}/stream/contents/feed/{uuid.uuid4()}", headers=auth(d.token))
    assert r.json()["items"] == []


async def test_stream_items_ids(client, session, user):
    d = await seed(session, user)
    r = await client.get(
        f"{API}/stream/items/ids",
        headers=auth(d.token),
        params={
            "s": READING_LIST,
            "n": 1000,
            "xt": READ,
            "includeAllDirectStreamIds": "true",
            "output": "json",
        },
    )
    refs = r.json()["itemRefs"]
    assert [ref["id"] for ref in refs] == [str(item_long_id(i.id)) for i in (d.b2, d.a3, d.a1)]
    assert refs[0]["directStreamIds"] == [f"feed/{d.feed_b.id}"]
    assert refs[0]["timestampUsec"] == str(to_usec(d.b2.published_at))
    assert "continuation" not in r.json()

    r = await client.get(f"{API}/stream/items/ids", headers=auth(d.token), params={"s": STARRED, "n": 1})
    assert [ref["id"] for ref in r.json()["itemRefs"]] == [str(item_long_id(d.c1.id))]
    assert r.json()["continuation"]
    r = await client.get(
        f"{API}/stream/items/ids",
        headers=auth(d.token),
        params={"s": STARRED, "n": 1, "c": r.json()["continuation"]},
    )
    assert [ref["id"] for ref in r.json()["itemRefs"]] == [str(item_long_id(d.a3.id))]

    r = await client.get(
        f"{API}/stream/items/ids", headers=auth(d.token), params={"s": f"feed/{d.feed_c.id}"}
    )
    assert len(r.json()["itemRefs"]) == 1


async def test_item_id_hex_decimal_roundtrip(client, session, user):
    d = await seed(session, user)
    hex_id = item_hex_id(d.a1.id)
    long = item_long_id(d.a1.id)
    assert len(hex_id) == 16 and int(hex_id, 16) == long and 0 <= long < 2**63
    assert parse_item_id(ITEM_TAG_PREFIX + hex_id) == long
    assert parse_item_id(str(long)) == long
    assert parse_item_id("garbage") is None

    # SQL derivation agrees with the Python derivation.
    row = await session.scalar(select(models.Item.id).where(long_id_sql(models.Item.id) == long))
    assert row == d.a1.id

    for form in (ITEM_TAG_PREFIX + hex_id, str(long)):
        r = await client.post(f"{API}/stream/items/contents", headers=auth(d.token), data={"i": form})
        assert r.status_code == 200
        assert [i["id"] for i in r.json()["items"]] == [ITEM_TAG_PREFIX + hex_id]

    r = await client.post(
        f"{API}/stream/items/contents",
        headers=auth(d.token),
        data={"i": [str(item_long_id(d.a1.id)), item_tag_id(d.c1.id), str(item_long_id(d.b1.id))]},
    )
    assert _titles(r.json()) == ["Item c1", "Item a1"]  # b1 hidden; newest first

    r = await client.get(
        f"{API}/stream/items/contents", headers=auth(d.token), params={"i": item_tag_id(d.a2.id)}
    )
    assert _titles(r.json()) == ["Item a2"]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def test_edit_tag_read_star_kept_unread_and_labels(client, session, user):
    d = await seed(session, user)
    h = auth(d.token)
    a1 = item_tag_id(d.a1.id)

    r = await client.post(f"{API}/edit-tag", headers=h, data={"i": a1, "a": READ, "T": "whatever"})
    assert r.status_code == 200 and r.text == "OK" and r.headers["content-type"].startswith("text/plain")
    st = await state_of(session, user, d.a1)
    assert st is not None and st.is_read and st.read_at is not None

    await client.post(f"{API}/edit-tag", headers=h, data={"i": a1, "r": READ})
    st = await state_of(session, user, d.a1)
    assert st is not None and not st.is_read

    await client.post(f"{API}/edit-tag", headers=h, data={"i": a1, "a": STARRED})
    st = await state_of(session, user, d.a1)
    assert st.is_starred and st.starred_at is not None
    await client.post(f"{API}/edit-tag", headers=h, data={"i": a1, "r": STARRED})
    assert not (await state_of(session, user, d.a1)).is_starred

    # kept-unread on an already-read item flips it back to unread; the decimal id form works too.
    await client.post(f"{API}/edit-tag", headers=h, data={"i": str(item_long_id(d.a2.id)), "a": KEPT_UNREAD})
    assert not (await state_of(session, user, d.a2)).is_read

    # Multiple items, multiple actions in one call.
    await client.post(
        f"{API}/edit-tag",
        headers=h,
        data={"i": [a1, item_tag_id(d.b2.id)], "a": [READ, "user/-/label/later"]},
    )
    assert (await state_of(session, user, d.a1)).tags == ["later"]
    assert (await state_of(session, user, d.b2)).tags == ["later"]  # no duplicate
    assert (await state_of(session, user, d.b2)).is_read

    await client.post(f"{API}/edit-tag", headers=h, data={"i": a1, "r": "user/-/label/later"})
    assert (await state_of(session, user, d.a1)).tags == []

    r = await client.get(
        f"{API}/stream/contents/{quote(READING_LIST, safe='')}", headers=h, params={"xt": READ}
    )
    assert _titles(r.json()) == ["Item a3", "Item a2"]


async def test_mark_all_as_read_with_ts(client, session, user):
    d = await seed(session, user)
    h = auth(d.token)
    ts = to_usec(d.a2.published_at)  # a1 and a2 are at/before this
    r = await client.post(f"{API}/mark-all-as-read", headers=h, data={"s": READING_LIST, "ts": str(ts)})
    assert r.status_code == 200 and r.text == "OK"
    assert (await state_of(session, user, d.a1)).is_read
    assert (await state_of(session, user, d.a2)).is_read
    assert not (await state_of(session, user, d.a3)).is_read
    assert (await state_of(session, user, d.b2)).is_read is False

    r = await client.get(f"{API}/unread-count", headers=h)
    assert {c["id"]: c["count"] for c in r.json()["unreadcounts"]}[READING_LIST] == 2

    # Without ts the whole stream is marked; scoped to the given feed only.
    await client.post(f"{API}/mark-all-as-read", headers=h, data={"s": f"feed/{d.feed_a.id}"})
    assert (await state_of(session, user, d.a3)).is_read
    assert not (await state_of(session, user, d.b2)).is_read

    await client.post(f"{API}/mark-all-as-read", headers=h, data={"s": "user/-/label/News"})
    assert (await state_of(session, user, d.b2)).is_read
    assert (await state_of(session, user, d.b1)).hidden and not (await state_of(session, user, d.b1)).is_read


async def test_subscription_edit_rename_move_unsubscribe(client, session, user):
    d = await seed(session, user)
    h = auth(d.token)
    fid = f"feed/{d.feed_a.id}"

    r = await client.post(
        f"{API}/subscription/edit", headers=h, data={"ac": "edit", "s": fid, "t": "Renamed A"}
    )
    assert r.status_code == 200 and r.text == "OK"
    assert (await fresh_feed(session, d.feed_a.id)).title == "Renamed A"

    # Move to a folder that does not exist yet: it is created.
    r = await client.post(
        f"{API}/subscription/edit",
        headers=h,
        data={"ac": "edit", "s": fid, "a": "user/-/label/Fresh", "r": "user/-/label/Tech"},
    )
    assert r.status_code == 200
    folder = await session.scalar(
        select(models.Folder).where(models.Folder.user_id == user.id, models.Folder.name == "Fresh")
    )
    assert folder is not None
    assert (await fresh_feed(session, d.feed_a.id)).folder_id == folder.id

    # Remove from folder (r only).
    await client.post(
        f"{API}/subscription/edit", headers=h, data={"ac": "edit", "s": fid, "r": "user/-/label/Fresh"}
    )
    assert (await fresh_feed(session, d.feed_a.id)).folder_id is None

    r = await client.post(f"{API}/subscription/edit", headers=h, data={"ac": "unsubscribe", "s": fid})
    assert r.status_code == 200
    assert await fresh_feed(session, d.feed_a.id) is None
    assert await session.scalar(select(models.Item).where(models.Item.feed_id == d.feed_a.id)) is None

    r = await client.post(
        f"{API}/subscription/edit", headers=h, data={"ac": "edit", "s": f"feed/{uuid.uuid4()}"}
    )
    assert r.status_code == 404


async def test_rename_and_disable_tag(client, session, user):
    d = await seed(session, user)
    h = auth(d.token)
    r = await client.post(
        f"{API}/rename-tag", headers=h, data={"s": "user/-/label/Tech", "dest": "user/-/label/Code"}
    )
    assert r.status_code == 200
    assert (
        await session.scalar(
            select(models.Folder)
            .where(models.Folder.id == d.tech.id)
            .execution_options(populate_existing=True)
        )
    ).name == "Code"

    r = await client.post(f"{API}/disable-tag", headers=h, data={"s": "user/-/label/News"})
    assert r.status_code == 200
    assert await session.scalar(select(models.Folder).where(models.Folder.id == d.news.id)) is None
    assert (await fresh_feed(session, d.feed_b.id)).folder_id is None

    # Tags (not folders) are edited on item_states.
    await client.post(
        f"{API}/rename-tag", headers=h, data={"s": "user/-/label/later", "dest": "user/-/label/someday"}
    )
    assert (await state_of(session, user, d.b2)).tags == ["someday"]
    await client.post(f"{API}/disable-tag", headers=h, data={"s": "user/-/label/someday"})
    assert (await state_of(session, user, d.b2)).tags == []


async def test_subscribe_and_quickadd_with_monkeypatched_add_feed(client, session, user, monkeypatch):
    d = await seed(session, user)
    h = auth(d.token)
    from pensieve.fetch import subscribe as subscribe_module

    calls: list[tuple[str, uuid.UUID | None]] = []

    async def fake_add_feed(session_, user_, url, folder_id=None):
        calls.append((url, folder_id))
        if "bad" in url:
            raise subscribe_module.FeedError("no feed found")
        feed = models.Feed(
            user_id=user_.id,
            url=url,
            site_url="https://new.example.com",
            title="New Feed",
            folder_id=folder_id,
        )
        session_.add(feed)
        await session_.flush()
        return feed

    monkeypatch.setattr(subscribe_module, "add_feed", fake_add_feed)

    r = await client.post(
        f"{API}/subscription/quickadd", headers=h, params={"quickadd": "https://new.example.com/rss"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["numResults"] == 1 and body["query"] == "https://new.example.com/rss"
    assert body["streamId"].startswith("feed/") and body["streamName"] == "New Feed"
    new_id = uuid.UUID(body["streamId"][5:])
    assert (await fresh_feed(session, new_id)).user_id == user.id

    r = await client.post(
        f"{API}/subscription/quickadd", headers=h, params={"quickadd": "https://bad.example.com"}
    )
    assert r.status_code == 200 and r.json()["numResults"] == 0 and "error" in r.json()

    r = await client.post(
        f"{API}/subscription/edit",
        headers=h,
        data={
            "ac": "subscribe",
            "s": "feed/https://two.example.com/rss",
            "t": "Two",
            "a": "user/-/label/Brand New",
        },
    )
    assert r.status_code == 200 and r.text == "OK"
    assert calls[-1][0] == "https://two.example.com/rss" and calls[-1][1] is not None
    feed = await session.scalar(select(models.Feed).where(models.Feed.url == "https://two.example.com/rss"))
    assert feed.title == "Two"
    folder = await session.get(models.Folder, calls[-1][1])
    assert folder.name == "Brand New" and folder.user_id == user.id

    r = await client.post(
        f"{API}/subscription/edit", headers=h, data={"ac": "subscribe", "s": "https://bad.example.com"}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


async def test_second_user_sees_nothing(client, session, user):
    d = await seed(session, user)
    _other, other_token, _ = await make_second_user(session)
    h = auth(other_token)

    r = await client.get(f"{API}/subscription/list", headers=h)
    assert [s["title"] for s in r.json()["subscriptions"]] == ["O"]
    r = await client.get(f"{API}/tag/list", headers=h)
    assert [t["id"] for t in r.json()["tags"]] == [STARRED]
    r = await client.get(f"{API}/unread-count", headers=h)
    assert {c["id"]: c["count"] for c in r.json()["unreadcounts"]}[READING_LIST] == 1
    r = await client.get(f"{API}/stream/contents/{quote(READING_LIST, safe='')}", headers=h)
    assert _titles(r.json()) == ["Item o1"]
    r = await client.get(f"{API}/stream/contents/feed/{d.feed_a.id}", headers=h)
    assert r.json()["items"] == []
    r = await client.post(f"{API}/stream/items/contents", headers=h, data={"i": item_tag_id(d.a1.id)})
    assert r.json()["items"] == []

    # Writes against the first user's items are no-ops.
    r = await client.post(f"{API}/edit-tag", headers=h, data={"i": item_tag_id(d.a1.id), "a": READ})
    assert r.status_code == 200
    assert await state_of(session, user, d.a1) is None
    await client.post(f"{API}/mark-all-as-read", headers=h, data={"s": READING_LIST})
    assert await state_of(session, user, d.a1) is None
    r = await client.post(
        f"{API}/subscription/edit", headers=h, data={"ac": "unsubscribe", "s": f"feed/{d.feed_a.id}"}
    )
    assert r.status_code == 404
    assert await fresh_feed(session, d.feed_a.id) is not None


def test_parse_stream_normalises_user_segment():
    assert parse_stream("user/1234/state/com.google/reading-list").kind == "reading-list"
    assert parse_stream("user/-/label/Tech").value == "Tech"
    assert parse_stream("feed/abc").value == "abc"
    assert parse_stream("").kind == "reading-list"
    assert parse_stream("user/-/state/com.google/broadcast").kind == "unknown"
