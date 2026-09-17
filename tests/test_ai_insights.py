# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

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
