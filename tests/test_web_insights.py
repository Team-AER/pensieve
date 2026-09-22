
from sqlalchemy import select

from pensieve import models
from pensieve.web import insights as insights_mod
from tests.test_web_support import HX, login, memory_limiter, seed_feed, seed_item  # noqa: F401


async def test_insights_renders_stored_daily_digest(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    top = await seed_item(session, feed, "Kernel 7.0 lands")
    skip1 = await seed_item(session, feed, "Repost one")
    skip2 = await seed_item(session, feed, "Repost two")
    starred = await seed_item(session, feed, "Starred but unread")
    insight = models.Insight(
        user_id=user.id,
        kind="daily",
        period="2026-09-17",
        title="A quiet Thursday with one big kernel release",
        body={
            "summary": "Mostly kernel news; the rest can wait.",
            "top_stories": [
                {"cluster_id": None, "item_id": str(top.id), "title": top.title, "why": "You read every kernel release note.", "sources": 4, "tag": "linux", "affinity": 0.92}
            ],
            "safe_to_skip": {"count": 2, "story_count": 1, "item_ids": [str(skip1.id), str(skip2.id)], "reason": "Reposts of a story you already read."},
            "starred_unread": [{"item_id": str(starred.id), "title": starred.title}],
            "trends": [{"name": "Rust in kernel", "series": [1, 2, 2, 5, 8, 7, 9], "delta": 6}],
        },
    )
    session.add(insight)
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights/digest")
    assert r.status_code == 200
    assert "Thursday, 17 September" in r.text
    assert "Kernel 7.0 lands" in r.text and "You read every kernel release note." in r.text
    assert "4 sources" in r.text and "92% match" in r.text
    assert "Rust in kernel" in r.text and "<polyline" in r.text and "+6" in r.text
    assert "2 items" in r.text and "Mark these read" in r.text
    assert "Starred but unread" in r.text
    await session.refresh(insight)
    assert insight.opened_at is not None
    # Mark safe-to-skip items read.
    r = await client.post(f"/insights/{insight.id}/skip-read", headers=headers | HX)
    assert r.status_code == 200 and "Marked 2 items as read" in r.text
    for iid in (skip1.id, skip2.id):
        state = await session.get(models.ItemState, (user.id, iid))
        assert state is not None and state.is_read
    assert await session.get(models.ItemState, (user.id, top.id)) is None
    r = await client.get(f"/insights/{insight.id}")
    assert r.status_code == 200 and "Marked 2 items as read" in r.text


async def test_insights_empty_state_enqueues_digest(client, session, user, monkeypatch):
    calls = []

    async def enqueue(function, *args, **kwargs):
        calls.append((function, args))

    monkeypatch.setattr(insights_mod.queue, "enqueue", enqueue)
    headers = await login(client, user)
    r = await client.get("/insights/digest")
    assert r.status_code == 200 and "No digest yet." in r.text and "/insights/generate" in r.text
    r = await client.post("/insights/generate", data={"kind": "daily"}, headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/insights/digest?queued=1"
    assert calls == [("ai_daily_digest", (str(user.id),))]
    r = await client.get("/insights/weekly")
    assert r.status_code == 200 and "No review yet." in r.text


async def test_insights_scoped_by_user(client, session, user):
    from tests.test_web_support import make_user

    other = await make_user(session)
    theirs = models.Insight(user_id=other.id, kind="daily", period="2026-09-18", title="Not yours", body={})
    session.add(theirs)
    await session.commit()
    await login(client, user)
    r = await client.get("/insights/digest")
    assert "Not yours" not in r.text
    r = await client.get(f"/insights/{theirs.id}")
    assert r.status_code == 404


async def seed_paper(session, user):
    a, b = await seed_feed(session, user, "Alpha"), await seed_feed(session, user, "Beta")
    s1 = await seed_item(session, a, "Big launch")
    s2 = await seed_item(session, b, "Big launch again")
    single = await seed_item(session, a, "Lone AI post")
    phone = await seed_item(session, b, "Phone review")
    cluster = models.Cluster(user_id=user.id, headline="Big launch everywhere", window_start=s1.published_at, window_end=s2.published_at, canonical_item_id=s1.id, source_count=2, kind="story")
    session.add(cluster)
    await session.flush()
    session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=s1.id), models.ClusterItem(cluster_id=cluster.id, item_id=s2.id)])
    session.add_all([
        models.ItemAI(user_id=user.id, item_id=s1.id, tags=["ai"], confidences={"ai": 0.9}, summary="- Launch bullet\n- Two\n- Three\n\n**Why this matters to you**\n\nBecause you follow launches.\n"),
        models.ItemAI(user_id=user.id, item_id=single.id, tags=["ai"], confidences={"ai": 0.8}),
        models.ItemAI(user_id=user.id, item_id=phone.id, tags=["apple"], confidences={"apple": 0.9}),
    ])
    session.add(models.Tag(user_id=user.id, name="ai", kind="ai"))
    session.add(models.Tag(user_id=user.id, name="apple", kind="ai"))
    await session.commit()
    return {"a": a, "b": b, "s1": s1, "s2": s2, "single": single, "phone": phone, "cluster": cluster}


async def test_paper_compiles_on_open_and_renders_sections(client, session, user):
    w = await seed_paper(session, user)
    headers = await login(client, user)
    r = await client.get("/insights")
    assert r.status_code == 200 and "The paper" in r.text
    assert "3 stories from 4 items" in r.text
    assert r.text.index('id="sec-ai"') < r.text.index('id="sec-apple"')
    assert "Big launch everywhere" in r.text and "2 sources" in r.text
    assert "Launch bullet" in r.text and "Because you follow launches." in r.text
    assert "Big launch again" in r.text and "Lone AI post" in r.text and "no summary yet" in r.text
    assert 'id="paper-settings"' in r.text and 'name="section" value="ai"' in r.text
    edition = await session.scalar(select(models.Insight).where(models.Insight.user_id == user.id, models.Insight.kind == "paper"))
    assert edition is not None and edition.opened_at is not None
    # hide the story: gone from the edition and kept out on recompile
    key = f"c:{w['cluster'].id}"
    r = await client.post(f"/insights/paper/{edition.id}/hide", data={"key": key}, headers=headers | HX)
    assert r.status_code == 200 and "story-hidden" in r.headers.get("HX-Trigger", "")
    r = await client.get("/insights?refresh=1")
    assert "Big launch everywhere" not in r.text and "Lone AI post" in r.text
    # mark a story read through the paper
    r = await client.post(f"/insights/paper/{edition.id}/read", data={"key": f"i:{w['single'].id}"}, headers=headers | HX)
    assert r.status_code == 200 and 'class="pstory read"' in r.text
    state = await session.get(models.ItemState, (user.id, w["single"].id))
    assert state is not None and state.is_read
    # and a whole section
    r = await client.post(f"/insights/paper/{edition.id}/read", data={"section": "apple"}, headers=headers | HX)
    assert r.status_code == 200 and 'id="sec-apple"' in r.text
    state = await session.get(models.ItemState, (user.id, w["phone"].id))
    assert state is not None and state.is_read


async def test_paper_settings_reorder_hide_and_mute(client, session, user):
    w = await seed_paper(session, user)
    headers = await login(client, user)
    r = await client.post(
        "/insights/paper/settings",
        data={"group_by": "tag", "window_hours": "48", "per_section": "3", "min_sources": "2",
              "show_summaries": "1", "auto_sections": "1", "section": ["apple", "ai"],
              "section_on": ["apple", "ai"], "limit:ai": "1", "muted_feed": str(w["b"].id)},
        headers=headers,
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == "/insights?saved=1"
    await session.refresh(user)
    cfg = user.settings["paper"]
    assert cfg["window_hours"] == 48 and cfg["min_sources"] == 2 and cfg["muted_feeds"] == [str(w["b"].id)]
    assert cfg["sections"] == [{"key": "apple", "on": True, "limit": None}, {"key": "ai", "on": True, "limit": 1}]
    r = await client.get("/insights?saved=1")
    assert "Layout saved" in r.text
    # Beta is muted: the story lost a source and drops to "In brief" under min_sources=2; apple is gone entirely
    assert 'id="sec-apple"' not in r.text and "In brief" in r.text and "Big launch again" not in r.text
    r = await client.post("/insights/paper/hide-section", data={"key": "ai"}, headers=headers)
    assert r.status_code == 303
    await session.refresh(user)
    assert {s["key"]: s["on"] for s in user.settings["paper"]["sections"]}["ai"] is False
    r = await client.get("/insights")
    assert 'id="sec-ai"' not in r.text
    r = await client.post("/insights/paper/mute-feed", data={"feed_id": str(w["a"].id)}, headers=headers)
    assert r.status_code == 303
    await session.refresh(user)
    assert set(user.settings["paper"]["muted_feeds"]) == {str(w["a"].id), str(w["b"].id)}


async def test_paper_off_and_other_users_edition(client, session, user):
    from tests.test_web_support import make_user

    other = await make_user(session)
    theirs = models.Insight(user_id=other.id, kind="paper", period="2026-09-18", title="Their paper", body={"sections": []})
    session.add(theirs)
    user.settings = {"paper": False}
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights")
    assert r.status_code == 200 and "switched off" in r.text
    r = await client.get(f"/insights/{theirs.id}")
    assert r.status_code == 404
    r = await client.post(f"/insights/paper/{theirs.id}/hide", data={"key": "x"}, headers=headers)
    assert r.status_code == 404
