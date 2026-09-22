# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from datetime import timedelta

from sqlalchemy import select

from pensieve import models
from pensieve.ai import cluster
from pensieve.config import get_settings
from tests.test_ai_helpers import angle_for, gateway, make_embedding, make_feed, make_item, now  # noqa: F401

settings = get_settings()
THR = settings.cluster_similarity_threshold


async def two_feeds(session, user):
    a, b = make_feed(user, "Feed A"), make_feed(user, "Feed B")
    session.add_all([a, b])
    await session.flush()
    return a, b


async def clusters_for(session, user):
    return list(
        (await session.scalars(select(models.Cluster).where(models.Cluster.user_id == user.id))).all()
    )


async def members(session, c):
    return set(
        (
            await session.scalars(
                select(models.ClusterItem.item_id).where(models.ClusterItem.cluster_id == c.id)
            )
        ).all()
    )


async def test_exact_dedup_same_hash_across_feeds(session, user, gateway):
    a, b = await two_feeds(session, user)
    old = make_item(a, "Same post", "body", age=timedelta(hours=5), hash_="h" * 64)
    new = make_item(b, "Same post", "body", age=timedelta(hours=1), hash_="h" * 64)
    session.add_all([old, new])
    await session.commit()
    touched = await cluster.cluster_items(session, user, [new])
    await session.commit()
    assert len(touched) == 1
    c = touched[0]
    assert c.kind == "duplicate" and c.canonical_item_id == old.id and c.source_count == 2
    assert await members(session, c) == {old.id, new.id}
    assert gateway.chat_calls == []


async def test_dedup_ignores_items_outside_window(session, user, gateway):
    a, b = await two_feeds(session, user)
    old = make_item(
        a, "Same post", "body", age=timedelta(hours=settings.cluster_window_hours + 5), hash_="h" * 64
    )
    new = make_item(b, "Same post", "body", age=timedelta(hours=1), hash_="h" * 64)
    session.add_all([old, new])
    await session.commit()
    assert await cluster.cluster_items(session, user, [new]) == []


async def test_embedding_merge_creates_story_cluster(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Postgres 18 released with async IO", "…", age=timedelta(hours=3))
    y = make_item(b, "PostgreSQL 18 is out", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(0.95))])
    await session.commit()
    gateway.chat({"same_story": True, "headline": "PostgreSQL 18 released"})
    touched = await cluster.cluster_items(session, user, [y])
    await session.commit()
    c = touched[0]
    assert c.kind == "story" and c.source_count == 2 and c.headline == "PostgreSQL 18 released"
    assert c.canonical_item_id == x.id and await members(session, c) == {x.id, y.id}
    sims = {ci.item_id: ci.similarity for ci in (await session.scalars(select(models.ClusterItem))).all()}
    assert abs(sims[y.id] - 0.95) < 0.01
    assert gateway.chat_requests[0].headers["X-Workflow"] == "cluster"


async def test_headline_falls_back_to_canonical_title_when_llm_fails(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Canonical title", "…", age=timedelta(hours=3))
    y = make_item(b, "Other", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(0.97))])
    await session.commit()
    gateway.chat("garbage", "garbage", "garbage")
    (c,) = await cluster.cluster_items(session, user, [y])
    assert c.headline == "Canonical title"


async def test_below_threshold_not_merged(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "One thing", "…", age=timedelta(hours=3))
    y = make_item(b, "Another", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(THR - 0.1))])
    await session.commit()
    assert await cluster.cluster_items(session, user, [y]) == []
    assert gateway.chat_calls == []


async def test_borderline_pair_confirmed_by_llm(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Borderline A", "…", age=timedelta(hours=3))
    y = make_item(b, "Borderline B", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(THR + 0.02))])
    await session.commit()
    gateway.chat({"same_story": False, "headline": "Borderline A"})
    assert await cluster.cluster_items(session, user, [y]) == []
    assert len(gateway.chat_calls) == 1
    gateway.chat({"same_story": True, "headline": "Both borderline"})
    (c,) = await cluster.cluster_items(session, user, [y])
    assert c.headline == "Both borderline" and await members(session, c) == {x.id, y.id}


async def test_split_override_prevents_merge(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Same story", "…", age=timedelta(hours=3))
    y = make_item(b, "Same story again", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(0.99))])
    session.add(models.ClusterOverride(user_id=user.id, item_a=y.id, item_b=x.id, action="split"))
    await session.commit()
    gateway.chat({"same_story": True, "headline": "x"})
    assert await cluster.cluster_items(session, user, [y]) == []


async def test_merge_override_forces_merge(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Unrelated A", "…", age=timedelta(hours=3))
    y = make_item(b, "Unrelated B", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(0.1))])
    session.add(models.ClusterOverride(user_id=user.id, item_a=x.id, item_b=y.id, action="merge"))
    await session.commit()
    gateway.chat({"same_story": True, "headline": "Forced"})
    (c,) = await cluster.cluster_items(session, user, [y])
    assert await members(session, c) == {x.id, y.id}


async def test_extends_existing_cluster(session, user, gateway):
    a, b = await two_feeds(session, user)
    c_feed = make_feed(user, "Feed C")
    session.add(c_feed)
    await session.flush()
    x = make_item(a, "Story", "…", age=timedelta(hours=4))
    y = make_item(b, "Story again", "…", age=timedelta(hours=2))
    session.add_all([x, y])
    await session.flush()
    session.add_all([make_embedding(x, 0.0), make_embedding(y, angle_for(0.96))])
    await session.commit()
    gateway.chat({"same_story": True, "headline": "Story"})
    await cluster.cluster_items(session, user, [y])
    await session.commit()
    z = make_item(c_feed, "Story thrice", "…", age=timedelta(hours=1))
    session.add(z)
    await session.flush()
    session.add(make_embedding(z, angle_for(0.97)))
    await session.commit()
    await cluster.cluster_items(session, user, [z])
    await session.commit()
    clusters = await clusters_for(session, user)
    assert len(clusters) == 1 and clusters[0].source_count == 3
    assert await members(session, clusters[0]) == {x.id, y.id, z.id}


async def test_jaccard_fallback_without_embeddings(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Rust 1.90 released with new borrow checker", "…", age=timedelta(hours=3))
    y = make_item(b, "Rust 1.90 released: new borrow checker", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.commit()
    gateway.chat({"same_story": True, "headline": "Rust 1.90 released"})
    (c,) = await cluster.cluster_items(session, user, [y])
    assert c.kind == "story" and await members(session, c) == {x.id, y.id}


async def test_series_detection_same_feed(session, user, gateway):
    a, _ = await two_feeds(session, user)
    p1 = make_item(a, "Writing a database, Part 1", "…", age=timedelta(days=10))
    p2 = make_item(a, "Writing a Database Part 2", "…", age=timedelta(hours=1))
    session.add_all([p1, p2])
    await session.commit()
    (c,) = await cluster.cluster_items(session, user, [p2])
    assert c.kind == "series" and c.canonical_item_id == p1.id and await members(session, c) == {p1.id, p2.id}
    assert gateway.chat_calls == []


async def test_group_stories_toggle(session, user, gateway):
    a, b = await two_feeds(session, user)
    old = make_item(a, "Same", "b", hash_="h" * 64, age=timedelta(hours=2))
    new = make_item(b, "Same", "b", hash_="h" * 64)
    session.add_all([old, new])
    await session.commit()
    user.settings = {"group_stories": False}
    assert await cluster.cluster_items(session, user, [new]) == []


async def test_unmerge_writes_overrides_and_deletes_singleton(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Same", "b", hash_="h" * 64, age=timedelta(hours=2))
    y = make_item(b, "Same", "b", hash_="h" * 64)
    session.add_all([x, y])
    await session.commit()
    (c,) = await cluster.cluster_items(session, user, [y])
    await session.commit()
    await cluster.unmerge(session, user, y.id)
    await session.commit()
    assert await session.get(models.Cluster, c.id) is None
    overrides = (
        await session.scalars(select(models.ClusterOverride).where(models.ClusterOverride.user_id == user.id))
    ).all()
    assert (
        len(overrides) == 1
        and overrides[0].action == "split"
        and {overrides[0].item_a, overrides[0].item_b} == {x.id, y.id}
    )
    # re-clustering honours the split
    assert await cluster.cluster_items(session, user, [y]) == []


async def test_unmerge_keeps_cluster_with_two_left(session, user, gateway):
    a, b = await two_feeds(session, user)
    c_feed = make_feed(user, "Feed C")
    session.add(c_feed)
    await session.flush()
    x = make_item(a, "Same", "b", hash_="h" * 64, age=timedelta(hours=3))
    y = make_item(b, "Same", "b", hash_="h" * 64, age=timedelta(hours=2))
    z = make_item(c_feed, "Same", "b", hash_="h" * 64, age=timedelta(hours=1))
    session.add_all([x, y, z])
    await session.commit()
    (c,) = await cluster.cluster_items(session, user, [y, z])
    await session.commit()
    await cluster.unmerge(session, user, x.id)
    await session.commit()
    await session.refresh(c)
    assert c.source_count == 2 and c.canonical_item_id == y.id
    assert await members(session, c) == {y.id, z.id}


async def test_jaccard_regression_ios_siri_pair_merges(session, user, gateway):
    """Real pair that the old tokeniser (len > 2, gate 0.6) missed: "ai", "ios" are two/three-letter tokens."""
    a, b = await two_feeds(session, user)
    x = make_item(a, "iOS 27.2 expands Siri AI to these new languages", "…", age=timedelta(hours=20))
    y = make_item(b, "iOS 27.2 Adds Siri AI in New Languages", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.commit()
    j = cluster.jaccard(cluster.title_tokens(x.title), cluster.title_tokens(y.title))
    assert {"ai", "ios", "siri"} <= cluster.title_tokens(y.title)
    assert j >= settings.cluster_jaccard_confirm_threshold
    gateway.chat({"same_story": True, "headline": "iOS 27.2 brings Siri AI to new languages"})
    (c,) = await cluster.cluster_items(session, user, [y])
    await session.commit()
    assert c.kind == "story" and await members(session, c) == {x.id, y.id} and c.source_count == 2
    assert len(await clusters_for(session, user)) == 1


async def test_jaccard_confirm_band_asks_llm_and_honours_no(session, user, gateway):
    a, b = await two_feeds(session, user)
    # tokens: {rust, 1.90, released, borrow, checker} vs {rust, 1.90, released, async, closures, stabilised}
    x = make_item(a, "Rust 1.90 released with new borrow checker", "…", age=timedelta(hours=3))
    y = make_item(b, "Rust 1.90 released: async closures stabilised", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.commit()
    j = cluster.jaccard(cluster.title_tokens(x.title), cluster.title_tokens(y.title))
    assert settings.cluster_jaccard_confirm_threshold <= j < settings.cluster_jaccard_merge_threshold
    gateway.chat({"same_story": False, "headline": "x"})
    assert await cluster.cluster_items(session, user, [y]) == []
    assert len(gateway.chat_calls) == 1 and gateway.chat_calls[0]["reasoning_effort"] == "none"
    gateway.chat({"same_story": True, "headline": "Rust 1.90 released"})
    (c,) = await cluster.cluster_items(session, user, [y])
    assert await members(session, c) == {x.id, y.id}


async def test_jaccard_below_confirm_band_never_calls_llm(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Rust 1.90 released with new borrow checker", "…", age=timedelta(hours=3))
    y = make_item(b, "Postgres 18 ships async IO", "…", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.commit()
    assert await cluster.cluster_items(session, user, [y]) == [] and gateway.chat_calls == []


async def test_jaccard_ignores_titles_with_too_few_tokens(session, user, gateway):
    a, b = await two_feeds(session, user)
    x = make_item(a, "Weekly roundup", "links from feed A", age=timedelta(hours=3))
    y = make_item(b, "Weekly roundup", "links from feed B", age=timedelta(hours=1))
    session.add_all([x, y])
    await session.commit()
    assert await cluster.cluster_items(session, user, [y]) == [] and gateway.chat_calls == []


def test_title_tokens_keep_short_tokens():
    assert cluster.title_tokens("Apple M5 and the AI race on iOS") == {"apple", "m5", "ai", "race", "ios"}
    assert cluster.title_tokens("A B c") == set()  # single characters still drop; "a" is a stop word


async def test_load_overrides_latest_action_wins(session, user, gateway):
    a, b = await two_feeds(session, user)
    x, y = make_item(a, "X", "…"), make_item(b, "Y", "…")
    session.add_all([x, y])
    await session.flush()
    t0 = now()
    # deliberately insert the *later* split first so insertion/uuid order would give the wrong answer
    session.add(
        models.ClusterOverride(user_id=user.id, item_a=x.id, item_b=y.id, action="split", created_at=t0)
    )
    session.add(
        models.ClusterOverride(
            user_id=user.id, item_a=y.id, item_b=x.id, action="merge", created_at=t0 - timedelta(days=1)
        )
    )
    await session.commit()
    overrides = await cluster.load_overrides(session, user.id)
    assert overrides == {frozenset((x.id, y.id)): "split"}
