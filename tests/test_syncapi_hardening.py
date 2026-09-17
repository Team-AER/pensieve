"""Sync API hardening: Fever unread_recently_read / id caps, Google Reader sortid/firstitemmsec, hex-first id
parsing, the long-id expression index, updated timestamps and server-side mark-all-read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from pensieve import models
from pensieve.syncapi import fever
from pensieve.syncapi.common import (
    ITEM_TAG_PREFIX,
    Stream,
    item_long_id,
    long_id,
    long_id_sql,
    mark_stream_read,
    parse_item_id,
    to_msec,
)
from tests.test_syncapi_fever import _call, _ids
from tests.test_syncapi_greader import API
from tests.test_syncapi_helpers import _item, auth, seed, state_of

# ---------------------------------------------------------------------------
# Fever
# ---------------------------------------------------------------------------


async def test_fever_unread_recently_read_reverts_last_hour(client, session, user):
    d = await seed(session, user)
    # a2 was read "now" (seed); c1 too. Push c1's read_at back two hours: it must stay read.
    c1_state = await state_of(session, user, d.c1)
    c1_state.read_at = datetime.now(UTC) - timedelta(hours=2)
    await session.commit()

    body = await _call(client, d.fever_key, "unread_item_ids", unread_recently_read=1)
    assert body["auth"] == 1
    assert long_id(d.a2.id) in _ids(body["unread_item_ids"])
    assert long_id(d.c1.id) not in _ids(body["unread_item_ids"])
    assert not (await state_of(session, user, d.a2)).is_read
    assert (await state_of(session, user, d.c1)).is_read
    # Starred state is untouched by the revert.
    assert (await state_of(session, user, d.c1)).is_starred


async def test_fever_state_ids_are_capped_to_newest(client, session, user, monkeypatch):
    d = await seed(session, user)
    monkeypatch.setattr(fever, "MAX_STATE_IDS", 2)
    body = await _call(client, d.fever_key, "unread_item_ids")
    ids = _ids(body["unread_item_ids"])
    assert len(ids) == 2
    # Unread newest-first: b2, a3 (a1 is the oldest unread and falls off).
    assert ids == {long_id(d.b2.id), long_id(d.a3.id)}


# ---------------------------------------------------------------------------
# Google Reader
# ---------------------------------------------------------------------------


async def test_subscription_list_has_sortid_and_firstitemmsec(client, session, user):
    d = await seed(session, user)
    r = await client.get(f"{API}/subscription/list", headers=auth(d.token), params={"output": "json"})
    assert r.status_code == 200
    subs = {s["id"]: s for s in r.json()["subscriptions"]}
    a = subs[f"feed/{d.feed_a.id}"]
    assert len(a["sortid"]) == 8 and int(a["sortid"], 16) >= 0
    assert a["firstitemmsec"] == str(to_msec(d.a1.published_at))
    # Deterministic across calls, and distinct per feed.
    r2 = await client.get(f"{API}/subscription/list", headers=auth(d.token))
    assert {s["id"]: s["sortid"] for s in r2.json()["subscriptions"]} == {k: v["sortid"] for k, v in subs.items()}
    assert len({s["sortid"] for s in subs.values()}) == 3
    # A feed with no items reports 0.
    empty = models.Feed(user_id=user.id, url="https://e.example.com/rss", title="Empty")
    session.add(empty)
    await session.commit()
    r = await client.get(f"{API}/subscription/list", headers=auth(d.token))
    assert next(s for s in r.json()["subscriptions"] if s["id"] == f"feed/{empty.id}")["firstitemmsec"] == "0"


async def test_subscription_list_icon_url_points_at_cached_favicon(client, session, user):
    d = await seed(session, user)
    d.feed_a.icon_data = b"\x89PNG\r\n\x1a\n"
    d.feed_a.icon_content_type = "image/png"
    await session.commit()
    r = await client.get(f"{API}/subscription/list", headers=auth(d.token))
    subs = {s["id"]: s for s in r.json()["subscriptions"]}
    assert subs[f"feed/{d.feed_a.id}"]["iconUrl"].endswith(f"/favicons/{d.feed_a.id}")
    assert subs[f"feed/{d.feed_b.id}"]["iconUrl"] == ""  # nothing cached and no icon_url


def test_parse_item_id_prefers_hex_form():
    # 16 hex digits that happen to be all-decimal must be read as hex, not as a decimal long.
    assert parse_item_id("1234567890123456") == int("1234567890123456", 16)
    assert parse_item_id("00000000000000ff") == 255
    assert parse_item_id(ITEM_TAG_PREFIX + "00000000000000ff") == 255
    # Real decimal long ids are 18-19 digits and still parse as decimal.
    assert parse_item_id("928886309497948309") == 928886309497948309
    assert parse_item_id("12") == 12
    assert parse_item_id("not-an-id") is None


async def test_long_id_expression_index_is_used(client, session, user):
    """The inlined SQL of long_id_sql() must match the ix_items_long_id expression so the planner uses it."""
    d = await seed(session, user)
    stmt = (
        models.Item.__table__.select()
        .with_only_columns(models.Item.id)
        .where(long_id_sql(models.Item.id) == item_long_id(d.a1.id))
    )
    compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "%(" not in compiled and "$1" not in compiled  # no bind parameters in the long-id expression
    await session.execute(text("SET enable_seqscan = off"))
    plan_rows = await session.execute(text("EXPLAIN " + compiled))
    plan = "\n".join(row[0] for row in plan_rows)
    await session.execute(text("RESET enable_seqscan"))
    assert "ix_items_long_id" in plan, plan
    # And the lookup itself still resolves the right row.
    assert (await session.scalar(stmt)) == d.a1.id


async def test_item_updated_uses_updated_at(client, session, user):
    d = await seed(session, user)
    later = d.a1.published_at + timedelta(days=3)
    d.a1.updated_at = later
    await session.commit()
    r = await client.get(f"{API}/stream/contents", headers=auth(d.token), params={"n": "10"})
    items = {i["title"]: i for i in r.json()["items"]}
    assert items["Item a1"]["updated"] == int(later.timestamp())
    assert items["Item a1"]["published"] == int(d.a1.published_at.timestamp())
    assert items["Item a2"]["updated"] == items["Item a2"]["published"]


async def test_mark_stream_read_writes_server_side(client, session, user):
    d = await seed(session, user)
    session.add_all([_item(d.feed_c, f"bulk{n}", 20 + n) for n in range(30)])
    await session.commit()
    # Feed c: c1 already read (existing row updated), 30 fresh ones (rows inserted) -> 30 changed.
    count = await mark_stream_read(session, user.id, Stream("feed", str(d.feed_c.id)))
    await session.commit()
    assert count == 30
    unread_left = await session.scalar(
        text(
            "SELECT count(*) FROM items i LEFT JOIN item_states s ON s.item_id = i.id AND s.user_id = :u "
            "WHERE i.feed_id = :f AND coalesce(s.is_read, false) = false"
        ),
        {"u": user.id, "f": d.feed_c.id},
    )
    assert unread_left == 0
    c1 = await state_of(session, user, d.c1)
    assert c1.is_read and c1.is_starred  # existing row kept its star
    assert await mark_stream_read(session, user.id, Stream("feed", str(d.feed_c.id))) == 0
