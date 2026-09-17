# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from datetime import timedelta

import httpx
import pytest
from arq import Retry
from sqlalchemy import select

from pensieve import models, queue
from pensieve.ai import jobs
from pensieve.ai.jobs import CRON_JOBS, FUNCTIONS
from tests.test_ai_helpers import (
    gateway,  # noqa: F401
    make_feed,
    make_item,
)


def tagging(n):
    return {
        "items": [
            {"index": i, "tags": [{"name": "ai", "confidence": 0.9}], "content_type": "article"}
            for i in range(n)
        ]
    }


async def seed(session, user, n=2):
    feed = make_feed(user, "Feed")
    session.add(feed)
    await session.flush()
    items = [make_item(feed, f"Item {i}", "text " * 30) for i in range(n)]
    session.add_all(items)
    await session.commit()
    return feed, items


async def ai_job(session, kind, target_id):
    stmt = select(models.AIJob).where(models.AIJob.kind == kind, models.AIJob.target_id == target_id)
    rows = (await session.scalars(stmt.execution_options(populate_existing=True))).all()
    assert len(rows) == 1
    return rows[0]


def test_function_registry_matches_queue_contract():
    assert [f.__name__ for f in FUNCTIONS] == [
        queue.AI_PROCESS_NEW_ITEMS,
        queue.AI_FILE_FEED,
        queue.AI_DAILY_DIGEST,
        queue.AI_WEEKLY_REVIEW,
        queue.AI_REFRESH_PROFILE,
        queue.AI_SUMMARIZE_ITEM,
    ]
    names = {c.coroutine.__name__: c for c in CRON_JOBS}
    assert set(names) == {"ai_dispatch_daily", "ai_dispatch_weekly"}
    assert names["ai_dispatch_weekly"].weekday is not None


async def test_process_new_items_runs_all_steps_and_mirrors_done(session, user, gateway):
    feed, items = await seed(session, user)
    gateway.chat_by_workflow({"tag_items": tagging(2), "cluster": {"same_story": True, "headline": "h"}})
    await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "done" and row.attempts == 1 and row.started_at and row.finished_at
    assert row.tokens_in > 0 and row.tokens_out > 0 and row.last_error is None
    assert (
        len(
            (
                await session.scalars(
                    select(models.Embedding).where(models.Embedding.item_id.in_([i.id for i in items]))
                )
            ).all()
        )
        == 2
    )
    assert (
        len((await session.scalars(select(models.ItemAI).where(models.ItemAI.user_id == user.id))).all()) == 2
    )


async def test_process_new_items_notes_embeddings_unavailable(session, user, gateway):
    feed, items = await seed(session, user)
    gateway.embeddings(available=False)
    gateway.chat_by_workflow({"tag_items": tagging(2)})
    await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "done" and "embeddings unavailable" in (row.last_error or "")
    assert (
        len((await session.scalars(select(models.ItemAI).where(models.ItemAI.user_id == user.id))).all()) == 2
    )


async def test_process_new_items_defers_when_gateway_down(session, user, gateway):
    feed, items = await seed(session, user)
    gateway.health(ok=False)
    with pytest.raises(Retry) as exc:
        await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    assert exc.value.defer_score == int(timedelta(minutes=10).total_seconds() * 1000)
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "queued" and row.last_error == "gateway unhealthy" and row.run_after > row.created_at
    assert gateway.chat_calls == [] and gateway.embed_calls == []


async def test_process_new_items_retries_on_llm_error_then_finishes(session, user, gateway):
    feed, items = await seed(session, user)
    gateway.chat(httpx.Response(500, text="boom"))
    with pytest.raises(Retry):
        await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "queued" and "tag failed" not in (row.last_error or "") and "500" in row.last_error
    # final attempt: partial failure is recorded as done-with-note, not retried forever
    await jobs.ai_process_new_items({"job_try": jobs.MAX_TRIES}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "done" and row.attempts == jobs.MAX_TRIES and "tag failed" in row.last_error


async def test_file_feed_job(session, user, gateway):
    folder = models.Folder(user_id=user.id, name="AI")
    session.add(folder)
    feed, _ = await seed(session, user)
    gateway.chat({"folder": "AI", "new_folder": None, "confidence": 0.95})
    await jobs.ai_file_feed({"job_try": 1}, str(feed.id))
    row = await ai_job(session, "file_feed", feed.id)
    assert row.status == "done" and row.last_error == "suggested AI"
    await session.refresh(feed)
    assert feed.folder_id == folder.id


async def test_daily_digest_and_profile_jobs(session, user, gateway):
    _feed, _items = await seed(session, user)
    gateway.chat_by_workflow(
        {
            "digest": {
                "summary": "s",
                "top_stories": [{"cluster_ref": 1, "why": "w"}],
                "safe_to_skip_reason": "",
            },
            "profile": {"profile_text": "You read Feed."},
            "weekly_review": {"summary": "w", "highlights": [], "suggestions": []},
        }
    )
    await jobs.ai_daily_digest({"job_try": 1}, str(user.id))
    await jobs.ai_refresh_profile({"job_try": 1}, str(user.id))
    await jobs.ai_weekly_review({"job_try": 1}, str(user.id))
    kinds = {
        r.kind: r
        for r in (await session.scalars(select(models.AIJob).where(models.AIJob.user_id == user.id))).all()
    }
    assert {k for k, r in kinds.items() if r.status == "done"} == {"digest", "profile", "weekly"}
    insights = (await session.scalars(select(models.Insight).where(models.Insight.user_id == user.id))).all()
    assert {i.kind for i in insights} == {"daily", "weekly"}
    profile = await session.scalar(select(models.Profile).where(models.Profile.user_id == user.id))
    assert profile.body_text == "You read Feed."


async def test_summarize_item_job(session, user, gateway):
    _feed, items = await seed(session, user, n=1)
    gateway.chat({"bullets": ["a", "b", "c"], "why_it_matters": "why"})
    await jobs.ai_summarize_item({"job_try": 1}, str(user.id), str(items[0].id))
    row = await ai_job(session, "summarize", items[0].id)
    assert row.status == "done" and row.user_id == user.id
    ai = await session.scalar(select(models.ItemAI).where(models.ItemAI.item_id == items[0].id))
    assert ai.summary.startswith("- a")


async def test_jobs_skip_when_ai_disabled(session, user, gateway):
    feed, items = await seed(session, user)
    user.ai_enabled = False
    await session.commit()
    await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "done" and row.last_error == "ai disabled" and gateway.chat_calls == []


class FakeRedis:
    def __init__(self):
        self.calls = []

    async def enqueue_job(self, function, *args, _job_id=None, **kwargs):
        self.calls.append((function, args, _job_id))


async def test_dispatchers_enqueue_per_user(session, user):
    other = models.User(
        email="off@example.com", password_hash="x", settings={"digest": False, "memory": True}
    )
    session.add(other)
    await session.commit()
    redis = FakeRedis()
    n = await jobs.ai_dispatch_daily({"redis": redis})
    assert n == 1 and redis.calls[0][0] == queue.AI_DAILY_DIGEST and redis.calls[0][1] == (str(user.id),)
    assert redis.calls[0][2].startswith(f"{queue.AI_DAILY_DIGEST}:{user.id}:")
    redis = FakeRedis()
    await jobs.ai_dispatch_weekly({"redis": redis})
    funcs = sorted((c[0], c[1][0]) for c in redis.calls)
    assert funcs == sorted(
        [
            (queue.AI_WEEKLY_REVIEW, str(user.id)),
            (queue.AI_REFRESH_PROFILE, str(user.id)),
            (queue.AI_REFRESH_PROFILE, str(other.id)),
        ]
    )
