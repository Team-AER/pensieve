from pensieve import models
from pensieve.web import insights as insights_mod
from tests.test_web_support import HX, login, seed_feed, seed_item


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
    r = await client.get("/insights")
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
    r = await client.get("/insights")
    assert r.status_code == 200 and "No digest yet." in r.text and "/insights/generate" in r.text
    r = await client.post("/insights/generate", data={"kind": "daily"}, headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/insights?queued=1"
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
    r = await client.get("/insights")
    assert "Not yours" not in r.text
    r = await client.get(f"/insights/{theirs.id}")
    assert r.status_code == 404
