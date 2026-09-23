from datetime import UTC, datetime, timedelta

from pensieve import models, queue
from pensieve.ai import retry as ai_retry
from tests.test_web_support import login, memory_limiter, seed_feed, seed_item  # noqa: F401


def job(user, kind, target, status="failed", **kw):
    kw.setdefault("last_error", "boom")
    return models.AIJob(user_id=user.id, kind=kind, target_id=target, status=status, **kw)


async def test_reconcile_settles_failures_a_later_run_fixed(session, user):
    feed = await seed_feed(session, user, "Feed")
    tagged = await seed_item(session, feed, "Tagged already")
    untagged = await seed_item(session, feed, "Never tagged")
    lonely = await seed_item(session, feed, "No summary yet")
    session.add(models.ItemAI(user_id=user.id, item_id=tagged.id, tags=["x"], confidences={"x": 0.9}))
    process = job(user, "process_items", feed.id)
    summary = job(user, "summarize", lonely.id)
    filing = job(user, "file_feed", feed.id, created_at=datetime.now(UTC) - timedelta(hours=2))
    lost = job(
        user, "process_items", feed.id, status="queued", run_after=datetime.now(UTC) - timedelta(days=1)
    )
    session.add_all([process, summary, filing, lost, job(user, "file_feed", feed.id, status="done")])
    await session.commit()

    assert await ai_retry.reconcile(session, user.id) == 2  # the lost row fails, the filing job is superseded
    await session.commit()
    assert (process.status, summary.status, filing.status) == ("failed", "failed", "resolved")
    assert lost.status == "failed" and lost.last_error.startswith("lost from the queue")

    # The missing work gets done elsewhere (a backfill tags the item, the sweep writes the summary).
    session.add(models.ItemAI(user_id=user.id, item_id=untagged.id, tags=["y"], confidences={"y": 0.8}))
    session.add(models.ItemAI(user_id=user.id, item_id=lonely.id, tags=[], confidences={}, summary="- done"))
    await session.commit()
    assert await ai_retry.reconcile(session, user.id) == 3
    await session.commit()
    assert {process.status, summary.status, lost.status} == {"resolved"}


async def test_retry_button_requeues_missing_work_and_corrects_counts(client, session, user, monkeypatch):
    calls = []

    async def enqueue(function, *args, **kwargs):
        calls.append((function, args))

    monkeypatch.setattr(ai_retry.queue, "enqueue", enqueue)
    feed = await seed_feed(session, user, "Feed")
    untagged = await seed_item(session, feed, "Never tagged")
    no_summary = await seed_item(session, feed, "Still no summary")
    session.add_all([job(user, "process_items", feed.id), job(user, "summarize_items", no_summary.id)])
    session.add(job(user, "process_items", feed.id, status="done", last_error="embeddings unavailable"))
    await session.commit()
    headers = await login(client, user)

    r = await client.get("/manage/ai")
    assert r.status_code == 200 and "Retry 2 failed" in r.text and "Recent notes from finished jobs" in r.text

    r = await client.post("/manage/ai/retry", headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/manage/ai?msg=ai_retried"
    kinds = {f: a for f, a in calls}
    assert kinds[queue.AI_PROCESS_NEW_ITEMS] == (
        str(feed.id),
        [str(no_summary.id), str(untagged.id)],
    ) or kinds[queue.AI_PROCESS_NEW_ITEMS] == (str(feed.id), [str(untagged.id), str(no_summary.id)])
    assert str(no_summary.id) in kinds[queue.AI_SUMMARIZE_ITEMS][1]

    r = await client.get("/manage/ai?msg=ai_retried")
    assert (
        "Retrying the failed work" in r.text
        and "Retry " not in r.text.split("Recent notes")[0].split("Queue")[1]
    )
    assert "0 failed" in r.text

    # Nothing failed any more: the button says so instead of queueing empty work.
    calls.clear()
    r = await client.post("/manage/ai/retry", headers=headers)
    assert r.headers["location"] == "/manage/ai?msg=ai_nothing_to_retry" and calls == []
