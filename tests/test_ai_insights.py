# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from pensieve import models
from pensieve.ai import insights, service
from pensieve.config import get_settings
from tests.test_ai_helpers import (  # noqa: F401
    angle_for,
    gateway,
    make_embedding,
    make_feed,
    make_item,
    make_state,
    now,
)

settings = get_settings()


def today():
    return datetime.now(ZoneInfo(settings.timezone)).date()


async def seed_digest_world(session, user):
    """Two feeds: 'Good' (opened often) and 'Noise' (never opened, low affinity)."""
    good, noise = make_feed(user, "Good"), make_feed(user, "Noise")
    session.add_all([good, noise])
    await session.flush()
    # history: 6 good items opened (individual read_at), 6 noise items marked read in one batch
    hist_good = [make_item(good, f"Old good {i}", "k8s", age=timedelta(days=i + 2)) for i in range(6)]
    hist_noise = [make_item(noise, f"Old noise {i}", "x", age=timedelta(days=i + 2)) for i in range(6)]
    session.add_all(hist_good + hist_noise)
    await session.flush()
    batch = now() - timedelta(days=1)
    session.add_all(
        [
            make_state(user, it, read=True, read_at=now() - timedelta(hours=i + 30))
            for i, it in enumerate(hist_good)
        ]
    )
    session.add_all([make_state(user, it, read=True, read_at=batch) for it in hist_noise])
    session.add_all([make_embedding(it, 0.0) for it in hist_good])
    # today's candidates
    fresh_good = make_item(good, "Kubernetes 1.40 ships", "k8s news", age=timedelta(hours=2))
    fresh_good2 = make_item(noise, "Kubernetes 1.40 ships too", "k8s news", age=timedelta(hours=1))
    fresh_noise = make_item(noise, "Celebrity gossip", "x", age=timedelta(hours=3))
    session.add_all([fresh_good, fresh_good2, fresh_noise])
    await session.flush()
    session.add_all(
        [
            make_embedding(fresh_good, 0.0),
            make_embedding(fresh_good2, angle_for(0.98)),
            make_embedding(fresh_noise, angle_for(0.1)),
        ]
    )
    cluster = models.Cluster(
        user_id=user.id,
        headline="Kubernetes 1.40 released",
        window_start=fresh_good.published_at,
        window_end=fresh_good2.published_at,
        canonical_item_id=fresh_good.id,
        source_count=2,
        kind="story",
    )
    session.add(cluster)
    await session.flush()
    session.add_all(
        [
            models.ClusterItem(cluster_id=cluster.id, item_id=fresh_good.id),
            models.ClusterItem(cluster_id=cluster.id, item_id=fresh_good2.id),
        ]
    )
    session.add(
        models.ItemAI(
            user_id=user.id, item_id=fresh_good.id, tags=["kubernetes"], confidences={"kubernetes": 0.9}
        )
    )
    session.add(
        models.ItemAI(user_id=user.id, item_id=hist_good[0].id, tags=["kubernetes", "devops"], confidences={})
    )
    # starred long ago, never read
    stale = make_item(good, "Long read to revisit", "…", age=timedelta(days=20))
    session.add(stale)
    await session.flush()
    session.add(make_state(user, stale, starred=True, starred_at=now() - timedelta(days=20)))
    session.add(
        models.Profile(user_id=user.id, version=1, body_text="You follow kubernetes and devops closely.")
    )
    await session.commit()
    return {
        "good": good,
        "noise": noise,
        "cluster": cluster,
        "fresh_good": fresh_good,
        "fresh_noise": fresh_noise,
        "stale": stale,
    }


async def test_daily_digest_upserts_insight_with_body_shape(session, user, gateway):
    w = await seed_digest_world(session, user)
    gateway.chat(
        {
            "summary": "Kubernetes day.",
            "top_stories": [{"cluster_ref": 1, "why": "you follow k8s"}],
            "safe_to_skip_reason": "gossip",
        }
    )
    row = await insights.daily_digest(session, user, today())
    await session.commit()
    assert row.kind == "daily" and row.period == today().isoformat() and row.title
    body = row.body
    assert set(body) >= {"summary", "top_stories", "safe_to_skip", "starred_unread", "trends"}
    assert body["summary"] == "Kubernetes day."
    top = body["top_stories"][0]
    assert set(top) == {"cluster_id", "item_id", "title", "why", "sources", "tag", "affinity"}
    assert top["cluster_id"] == str(w["cluster"].id) and top["item_id"] == str(w["fresh_good"].id)
    assert top["sources"] == 2 and top["tag"] == "kubernetes" and top["why"] == "you follow k8s"
    assert top["affinity"] > 0.9  # cosine 1.0 with the centroid, boosted by the profile mention (capped at 1)
    skip = body["safe_to_skip"]
    assert set(skip) == {"count", "story_count", "item_ids", "reason"}
    assert (
        skip["item_ids"] == [str(w["fresh_noise"].id)] and skip["count"] == 1 and skip["reason"] == "gossip"
    )
    assert body["starred_unread"] == [{"item_id": str(w["stale"].id), "title": "Long read to revisit"}]
    assert (
        body["trends"] and body["trends"][0]["name"] == "kubernetes" and len(body["trends"][0]["series"]) == 4
    )
    assert "delta" in body["trends"][0]
    assert row.item_refs == [w["fresh_good"].id]
    req = gateway.chat_requests[0]
    assert req.headers["X-Workflow"] == "digest" and gateway.chat_calls[0]["model"] == settings.llm_long_model

    # upsert: same period rewrites the same row
    gateway.chat({"summary": "Second run.", "top_stories": [], "safe_to_skip_reason": ""})
    again = await insights.daily_digest(session, user, today())
    await session.commit()
    rows = (await session.scalars(select(models.Insight).where(models.Insight.user_id == user.id))).all()
    assert len(rows) == 1 and again.id == row.id and rows[0].body["summary"] == "Second run."


async def test_daily_digest_survives_llm_failure(session, user, gateway):
    await seed_digest_world(session, user)
    gateway.chat("nope", "nope", "nope")
    row = await insights.daily_digest(session, user, today())
    assert row is not None and row.body["summary"] and row.body["top_stories"][0]["why"] == ""


async def test_daily_digest_respects_toggle(session, user, gateway):
    user.settings = {"digest": False}
    assert await insights.daily_digest(session, user, today()) is None


async def test_feed_open_rates(session, user, gateway):
    w = await seed_digest_world(session, user)
    rates = await insights.feed_open_rates(session, user.id)
    assert rates[w["good"].id] > 0.5 and rates[w["noise"].id] == 0.0


async def test_weekly_review_stores_insight(session, user, gateway):
    w = await seed_digest_world(session, user)
    y, wk, _ = today().isocalendar()
    iso = f"{y}-W{wk:02d}"
    gateway.chat({"summary": "Busy week.", "highlights": ["k8s"], "suggestions": ["mute Noise"]})
    row = await insights.weekly_review(session, user, iso)
    await session.commit()
    assert row.kind == "weekly" and row.period == iso
    body = row.body
    assert body["summary"] == "Busy week." and body["suggestions"] == ["mute Noise"]
    assert "Inbox" in body["volume_by_folder"]
    signals = {s["title"]: s for s in body["source_signals"]}
    assert {"posts", "open_share", "skip_share", "topic_drift"} <= set(signals["Good"])
    assert (
        isinstance(body["rising"], list) and isinstance(body["fading"], list) and "most_read_sources" in body
    )
    assert gateway.chat_requests[0].headers["X-Workflow"] == "weekly_review"
    _ = w


async def test_summarize_item_stores_markdown(session, user, gateway):
    feed = make_feed(user, "Feed")
    session.add(feed)
    await session.flush()
    item = make_item(feed, "Big release", "lots of text " * 100)
    session.add(item)
    session.add(models.Profile(user_id=user.id, version=1, body_text="You care about releases."))
    await session.commit()
    gateway.chat({"bullets": ["one", "two", "three"], "why_it_matters": "Because releases."})
    md = await service.summarize_item(session, user, item)
    await session.commit()
    assert (
        md.startswith("- one\n- two\n- three")
        and "**Why this matters to you**" in md
        and "Because releases." in md
    )
    row = await session.scalar(
        select(models.ItemAI).where(models.ItemAI.item_id == item.id, models.ItemAI.user_id == user.id)
    )
    assert row.summary == md
    assert "You care about releases." in gateway.chat_calls[0]["messages"][1]["content"]
    assert gateway.chat_requests[0].headers["X-Workflow"] == "summarize_item"


def batch_summaries(indexes):
    return {
        "items": [{"index": i, "bullets": [f"b{i}", "x", "y"], "why_it_matters": f"why {i}"} for i in indexes]
    }


async def test_summarize_items_batches_and_skips_story_duplicates(session, user, gateway):
    feed_a, feed_b = make_feed(user, "A"), make_feed(user, "B")
    session.add_all([feed_a, feed_b])
    await session.flush()
    solo = [make_item(feed_a, f"Solo {i}", "text " * 20, age=timedelta(hours=i + 1)) for i in range(5)]
    dupe_a = make_item(feed_a, "Same story", "text", age=timedelta(hours=8))
    dupe_b = make_item(feed_b, "Same story again", "text", age=timedelta(hours=7))
    already = make_item(feed_b, "Has one", "text", age=timedelta(hours=9))
    session.add_all([*solo, dupe_a, dupe_b, already])
    await session.flush()
    cluster = models.Cluster(
        user_id=user.id, headline="Same story", window_start=dupe_a.published_at,
        window_end=dupe_b.published_at, canonical_item_id=dupe_a.id, source_count=2, kind="story",
    )  # fmt: skip
    session.add(cluster)
    await session.flush()
    session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=i.id) for i in (dupe_a, dupe_b)])
    session.add(models.ItemAI(user_id=user.id, item_id=already.id, summary="- old\n", tags=["ai"]))
    await session.commit()

    calls = []

    def side_effect(request):
        import json

        import httpx

        from tests.test_ai_helpers import chat_response

        body = json.loads(request.content)
        n = body["messages"][1]["content"].count("### Article ")
        calls.append(n)
        return httpx.Response(200, json=chat_response(batch_summaries(range(n))))

    gateway.router.post(f"{settings.llm_base_url.rstrip('/')}/chat/completions").mock(side_effect=side_effect)
    written = await insights.summarize_items(session, user, [*solo, dupe_a, dupe_b, already])
    await session.commit()
    # 5 solos + one member of the story (its canonical item) = 6 articles in batches of 4 + 2
    assert calls == [4, 2] and set(written) == {*[i.id for i in solo], dupe_a.id}
    rows = {
        r.item_id: r
        for r in (await session.scalars(select(models.ItemAI).where(models.ItemAI.user_id == user.id))).all()
    }
    assert dupe_b.id not in rows and rows[already.id].summary == "- old\n"
    assert (
        rows[dupe_a.id].summary.startswith("- b") and "**Why this matters to you**" in rows[dupe_a.id].summary
    )
    assert rows[solo[0].id].tags == [] and rows[solo[0].id].prompt_version == ""
    # the story is covered now: a member arriving later is not summarised again
    late = make_item(feed_b, "Same story, third copy", "text", age=timedelta(hours=6))
    session.add(late)
    await session.flush()
    session.add(models.ClusterItem(cluster_id=cluster.id, item_id=late.id))
    await session.commit()
    assert await insights.summarize_items(session, user, [late]) == {} and calls == [4, 2]
    req = gateway.chat_requests[0]
    assert (
        req.headers["X-Workflow"] == "summarize_items"
        and gateway.chat_calls[0]["model"] == settings.llm_fast_model
    )


async def test_summarize_items_retries_entries_the_model_missed(session, user, gateway):
    feed = make_feed(user, "A")
    session.add(feed)
    await session.flush()
    items = [make_item(feed, f"Item {i}", "text " * 20, age=timedelta(hours=i + 1)) for i in range(4)]
    session.add_all(items)
    await session.commit()
    calls = []

    def side_effect(request):
        import json

        import httpx

        from tests.test_ai_helpers import chat_response

        body = json.loads(request.content)
        n = body["messages"][1]["content"].count("### Article ")
        calls.append(n)
        if len(calls) == 1:
            # first batch of 4: index 1 dropped, index 3 malformed (two bullets instead of three)
            payload = batch_summaries([0, 2])
            payload["items"].append({"index": 3, "bullets": ["only", "two"], "why_it_matters": "w"})
            return httpx.Response(200, json=chat_response(payload))
        return httpx.Response(200, json=chat_response(batch_summaries(range(n))))

    gateway.router.post(f"{settings.llm_base_url.rstrip('/')}/chat/completions").mock(side_effect=side_effect)
    written = await insights.summarize_items(session, user, items)
    await session.commit()
    # one batch of four, then the two missed entries again in one batch of two
    assert calls == [4, 2] and set(written) == {i.id for i in items}
    rows = {
        r.item_id: r
        for r in (await session.scalars(select(models.ItemAI).where(models.ItemAI.user_id == user.id))).all()
    }
    assert rows[items[1].id].summary.startswith("- b0") and rows[items[3].id].summary.startswith("- b1")


async def test_summary_prefs_shape_the_prompt_and_the_markdown(session, user, gateway):
    feed = make_feed(user, "A")
    session.add(feed)
    await session.flush()
    item = make_item(feed, "Item", "text " * 20, age=timedelta(hours=1))
    session.add(item)
    user.settings = {"summaries": {"bullets": "5", "why": "off", "focus": "  homelab   and self-hosting "}}
    await session.commit()
    assert insights.summary_prefs(user) == {"bullets": 5, "why": "off", "focus": "homelab and self-hosting"}
    assert insights.summary_prefs(models.User(settings={"summaries": {"bullets": 4, "why": "x"}})) == {
        "bullets": 3,
        "why": "personal",
        "focus": "",
    }
    gateway.chat({"items": [{"index": 0, "bullets": ["1", "2", "3", "4", "5"], "why_it_matters": ""}]})
    written = await insights.summarize_items(session, user, [item])
    assert written[item.id] == "- 1\n- 2\n- 3\n- 4\n- 5\n" and "Why" not in written[item.id]
    call = gateway.chat_calls[0]
    assert "exactly five crisp bullets" in call["messages"][0]["content"]
    assert "why_it_matters to an empty string" in call["messages"][0]["content"]
    assert "homelab and self-hosting" in call["messages"][1]["content"]
    assert (
        call["response_format"]["json_schema"]["schema"]["properties"]["items"]["items"]["properties"][
            "bullets"
        ]["minItems"]
        == 5
    )
    # the general mode ignores the profile for the why, and the why is kept
    user.settings = {"summaries": {"why": "general"}}
    gateway.chat({"bullets": ["a", "b", "c"], "why_it_matters": "matters"})
    md = await insights.summarize_item(session, user, item)
    assert md.endswith("**Why this matters to you**\n\nmatters\n")
    assert (
        "technical reader in general (ignore the reader profile"
        in gateway.chat_calls[-1]["messages"][0]["content"]
    )


async def test_items_without_summary_is_story_aware(session, user, gateway):
    feed_a, feed_b = make_feed(user, "A"), make_feed(user, "B")
    session.add_all([feed_a, feed_b])
    await session.flush()
    done = make_item(feed_a, "Done", "t", age=timedelta(hours=1))
    twin = make_item(feed_b, "Done twin", "t", age=timedelta(hours=2))
    open_a = make_item(feed_a, "Open a", "t", age=timedelta(hours=3))
    open_b = make_item(feed_b, "Open b", "t", age=timedelta(hours=4))
    old = make_item(feed_a, "Old", "t", age=timedelta(days=5))
    session.add_all([done, twin, open_a, open_b, old])
    await session.flush()
    c1 = models.Cluster(user_id=user.id, headline="d", window_start=now(), window_end=now(), canonical_item_id=done.id, source_count=2, kind="story")  # fmt: skip
    c2 = models.Cluster(user_id=user.id, headline="o", window_start=now(), window_end=now(), canonical_item_id=open_a.id, source_count=2, kind="story")  # fmt: skip
    session.add_all([c1, c2])
    await session.flush()
    session.add_all([
        models.ClusterItem(cluster_id=c1.id, item_id=done.id), models.ClusterItem(cluster_id=c1.id, item_id=twin.id),
        models.ClusterItem(cluster_id=c2.id, item_id=open_a.id), models.ClusterItem(cluster_id=c2.id, item_id=open_b.id),
    ])  # fmt: skip
    session.add(models.ItemAI(user_id=user.id, item_id=done.id, summary="- x\n"))
    await session.commit()
    ids = await insights.items_without_summary(session, user.id, now() - timedelta(days=1))
    assert ids == [open_a.id]  # the twin's story is covered; one member per open story; old is out of range
    assert await insights.items_without_summary(
        session, user.id, now() - timedelta(days=1), until=now() - timedelta(hours=3, minutes=30)
    ) == [open_b.id]
    assert (
        await insights.items_without_summary(
            session, user.id, now() - timedelta(days=1), until=now() - timedelta(hours=4, minutes=30)
        )
        == []
    )


async def test_digest_without_embeddings_ranks_by_open_rate_and_tags(session, user, gateway):
    """No centroid and no item vectors: affinity comes from feed open-rate + tag overlap, not a constant."""
    w = await seed_digest_world(session, user)
    await session.execute(delete(models.Embedding))
    # a third, never-opened feed with a very recent item: recency alone would rank it first
    dull = make_feed(user, "Dull")
    session.add(dull)
    await session.flush()
    dull_hist = [make_item(dull, f"Dull old {i}", "x", age=timedelta(days=i + 2)) for i in range(6)]
    session.add_all(dull_hist)
    await session.flush()
    batch = now() - timedelta(days=1, hours=1)
    session.add_all([make_state(user, it, read=True, read_at=batch) for it in dull_hist])
    dull_fresh = make_item(dull, "Dull but newest", "x", age=timedelta(minutes=5))
    session.add(dull_fresh)
    await session.commit()
    gateway.chat({"summary": "s", "top_stories": [], "safe_to_skip_reason": "rarely opened"})
    row = await insights.daily_digest(session, user, today())
    body = row.body
    skipped = set(body["safe_to_skip"]["item_ids"])
    assert {
        str(w["fresh_noise"].id),
        str(dull_fresh.id),
    } <= skipped  # low open-rate feeds land in safe_to_skip
    top_ids = [s["item_id"] for s in body["top_stories"]]
    assert top_ids[0] == str(w["fresh_good"].id) and str(dull_fresh.id) not in top_ids
    affinities = {s["item_id"]: s["affinity"] for s in body["top_stories"]}
    assert affinities[str(w["fresh_good"].id)] > insights.LOW_AFFINITY  # open-rate 1.0 + kubernetes overlap
    sent = gateway.chat_calls[0]["messages"][1]["content"]
    assert "Dull but newest" in sent and "Celebrity gossip" in sent  # skip candidates are still listed
    assert gateway.chat_calls[0]["reasoning_effort"] == settings.llm_digest_reasoning


async def test_top_tags_weights_from_history(session, user, gateway):
    await seed_digest_world(session, user)
    assert await insights.top_tags(session, user.id) == {"kubernetes": 1.0, "devops": 1.0}
    assert await insights.top_tags(session, user.id, days=0) == {}
