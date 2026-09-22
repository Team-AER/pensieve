# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from arq import Retry
from sqlalchemy import select

from pensieve import models, queue
from pensieve.ai import jobs
from pensieve.ai.jobs import CRON_JOBS, FUNCTIONS
from pensieve.config import get_settings
from tests.test_ai_helpers import (
    gateway,  # noqa: F401
    make_feed,
    make_item,
    now,
)

settings = get_settings()


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


@pytest.fixture(autouse=True)
def enqueued(monkeypatch):
    """Record what a job hands to the queue when its ctx carries no arq redis (never touch a real Redis)."""
    calls = []

    async def fake_enqueue(function, *args, _job_id=None, **kwargs):
        calls.append((function, args, _job_id))

    monkeypatch.setattr(jobs.queue, "enqueue", fake_enqueue)
    return calls


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
        queue.AI_SUMMARIZE_ITEMS,
        queue.AI_DAILY_PAPER,
    ]
    names = {c.coroutine.__name__: c for c in CRON_JOBS}
    assert set(names) == {"ai_dispatch_daily", "ai_dispatch_weekly"}
    assert names["ai_dispatch_weekly"].weekday == 6 and names["ai_dispatch_weekly"].hour == 8
    assert names["ai_dispatch_daily"].minute == {0, 15, 30, 45} and names["ai_dispatch_daily"].hour is None
    from pensieve.worker import AIWorkerSettings, WorkerSettings

    assert WorkerSettings.timezone == ZoneInfo(settings.timezone)
    assert AIWorkerSettings.job_timeout == jobs.JOB_TIMEOUT_S
    assert AIWorkerSettings.queue_name == "pensieve:ai" and WorkerSettings.queue_name == "arq:queue"


async def test_process_new_items_runs_all_steps_and_mirrors_done(session, user, gateway, enqueued):
    feed, items = await seed(session, user)
    gateway.chat_by_workflow({"tag_items": tagging(2), "cluster": {"same_story": True, "headline": "h"}})
    await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "done" and row.attempts == 1 and row.started_at and row.finished_at
    # eager summaries are a follow-up job of their own, queued once tagging and clustering are committed
    assert [(c[0], c[1][0], sorted(c[1][1])) for c in enqueued] == [
        (queue.AI_SUMMARIZE_ITEMS, str(user.id), sorted(str(i.id) for i in items))
    ]
    assert enqueued[0][2].startswith(f"{queue.AI_SUMMARIZE_ITEMS}:{feed.id}:")
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
    # final attempt: embeddings + clustering worked, tagging did not -> "partial", never "done"
    await jobs.ai_process_new_items({"job_try": jobs.MAX_TRIES}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "partial" and row.attempts == jobs.MAX_TRIES and "tag failed" in row.last_error


async def test_process_new_items_all_steps_failing_is_failed(session, user, gateway):
    feed, items = await seed(session, user)
    gateway.embeddings(available=True)
    gateway.router.post(f"{settings.llm_base_url.rstrip('/')}/embeddings").mock(
        return_value=httpx.Response(500, text="boom")
    )
    gateway.chat(httpx.Response(500, text="boom"))
    await jobs.ai_process_new_items({"job_try": jobs.MAX_TRIES}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "failed" and "embed failed" in row.last_error and "tag failed" in row.last_error


async def test_mirror_rows_keyed_by_arq_job_id(session, user, gateway):
    feed, items = await seed(session, user, n=4)
    gateway.chat_by_workflow({"tag_items": tagging(2)})
    a, b = [str(i.id) for i in items[:2]], [str(i.id) for i in items[2:]]
    await jobs.ai_process_new_items({"job_try": 1, "job_id": "backfill:x:0"}, str(feed.id), a)
    await jobs.ai_process_new_items({"job_try": 1, "job_id": "backfill:x:1"}, str(feed.id), b)
    rows = (
        await session.scalars(
            select(models.AIJob).where(models.AIJob.kind == "process_items", models.AIJob.target_id == feed.id)
        )
    ).all()
    assert len(rows) == 2 and {r.id for r in rows} == {
        jobs.mirror_id({"job_id": "backfill:x:0"}, "process_items", feed.id),
        jobs.mirror_id({"job_id": "backfill:x:1"}, "process_items", feed.id),
    }
    # a retry of the same job id updates its own row
    await jobs.ai_process_new_items({"job_try": 2, "job_id": "backfill:x:0"}, str(feed.id), a)
    rows = (
        await session.scalars(
            select(models.AIJob)
            .where(models.AIJob.kind == "process_items")
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(rows) == 2 and {r.attempts for r in rows} == {1, 2}


async def test_cap_remainder_is_requeued_in_chunks(session, user, gateway, monkeypatch):
    feed, items = await seed(session, user, n=5)
    monkeypatch.setattr(settings, "ai_max_items_per_job", 2)
    gateway.chat_by_workflow({"tag_items": tagging(2)})
    redis = FakeRedis()
    await jobs.ai_process_new_items({"job_try": 1, "redis": redis}, str(feed.id), [str(i.id) for i in items])
    row = await ai_job(session, "process_items", feed.id)
    assert row.status == "done" and "3 more re-queued in 2 job(s)" in row.last_error
    newest_first = sorted(items, key=lambda i: i.published_at, reverse=True)
    expected = [[str(i.id) for i in newest_first[2:4]], [str(newest_first[4].id)]]
    process = [c for c in redis.calls if c[0] == queue.AI_PROCESS_NEW_ITEMS]
    assert len(process) == 2 and [c[0] for c in redis.calls][-1] == queue.AI_SUMMARIZE_ITEMS
    assert [list(c[1][1]) for c in process] == expected
    assert [c[2] for c in process] == [jobs.remainder_job_id(feed.id, n, ch) for n, ch in enumerate(expected, 1)]
    assert all(c[2].startswith(f"process:{feed.id}:") for c in process)
    # summaries are queued for the two items this job actually processed
    assert sorted(redis.calls[-1][1][1]) == sorted(str(i.id) for i in newest_first[:2])
    # only the newest two were tagged by this job
    tagged = (await session.scalars(select(models.ItemAI.item_id).where(models.ItemAI.user_id == user.id))).all()
    assert set(tagged) == {i.id for i in newest_first[:2]}


async def test_reaper_fails_stale_running_rows(session, user, gateway):
    stale = models.AIJob(
        kind="digest", user_id=user.id, status="running", started_at=now() - timedelta(seconds=jobs.JOB_TIMEOUT_S + 5)
    )
    fresh = models.AIJob(kind="digest", user_id=user.id, status="running", started_at=now())
    session.add_all([stale, fresh])
    await session.commit()
    assert await jobs.reap_stale_jobs() == 1
    await session.refresh(stale)
    await session.refresh(fresh)
    assert stale.status == "failed" and "reaped" in stale.last_error and stale.finished_at is not None
    assert fresh.status == "running"


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


async def test_process_new_items_respects_summaries_toggle(session, user, gateway, enqueued):
    feed, items = await seed(session, user)
    user.settings = {"summarize_items": False}
    await session.commit()
    gateway.chat_by_workflow({"tag_items": tagging(2)})
    await jobs.ai_process_new_items({"job_try": 1}, str(feed.id), [str(i.id) for i in items])
    assert enqueued == []


def summaries(n):
    return {"items": [{"index": i, "bullets": ["a", "b", "c"], "why_it_matters": f"why {i}"} for i in range(n)]}


async def test_summarize_items_job(session, user, gateway):
    _feed, items = await seed(session, user, n=3)
    gateway.chat_by_workflow({"summarize_items": summaries(3)})
    await jobs.ai_summarize_items({"job_try": 1}, str(user.id), [str(i.id) for i in items])
    row = await ai_job(session, "summarize_items", items[0].id)
    assert row.status == "done" and row.user_id == user.id and "summarised 3 of 3" in row.last_error
    rows = (await session.scalars(select(models.ItemAI).where(models.ItemAI.user_id == user.id))).all()
    assert len(rows) == 3 and all(r.summary and r.summary.startswith("- a") for r in rows)
    assert all(r.tags == [] and r.prompt_version == "" for r in rows)  # summary-only rows still need tagging
    # a second run has nothing to do and makes no model call
    before = len(gateway.chat_calls)
    await jobs.ai_summarize_items({"job_try": 1, "job_id": "again"}, str(user.id), [str(i.id) for i in items])
    assert len(gateway.chat_calls) == before


async def test_daily_paper_job(session, user, gateway):
    _feed, items = await seed(session, user, n=2)
    session.add(models.ItemAI(user_id=user.id, item_id=items[0].id, tags=["ai"], confidences={"ai": 0.9}))
    await session.commit()
    await jobs.ai_daily_paper({"job_try": 1}, str(user.id))
    row = await ai_job(session, "paper", user.id)
    assert row.status == "done" and "2 stories" in row.last_error and gateway.chat_calls == []
    edition = await session.scalar(select(models.Insight).where(models.Insight.kind == "paper"))
    assert edition is not None and [s["key"] for s in edition.body["sections"]] == ["ai", "other"]


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


def at_local(hour: int, minute: int) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone)).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def test_dispatchers_enqueue_per_user(session, user):
    other = models.User(
        email="off@example.com", password_hash="x", settings={"digest": False, "memory": True}
    )
    session.add(other)
    await session.commit()
    redis = FakeRedis()
    when = at_local(settings.digest_hour_local, settings.digest_minute_local)
    n = await jobs.ai_dispatch_daily({"redis": redis}, at=when)
    assert n == 1 and redis.calls[0][0] == queue.AI_DAILY_DIGEST and redis.calls[0][1] == (str(user.id),)
    assert redis.calls[0][2] == jobs.digest_job_id(user.id, when.date())
    assert redis.calls[0][2].startswith(f"{queue.AI_DAILY_DIGEST}:{user.id}:")
    # the paper is compiled at digest time too, so the edition is on record even if nobody opens it
    assert [c[0] for c in redis.calls] == [queue.AI_DAILY_DIGEST, queue.AI_DAILY_PAPER]
    assert redis.calls[1][2] == jobs.paper_job_id(user.id, when.date())
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


async def test_dispatch_daily_honours_per_user_digest_time(session, user):
    early = models.User(email="early@example.com", password_hash="x", settings={"digest_time": "06:10"})
    session.add(early)
    user.settings = {"digest_time": "21:50"}
    await session.commit()
    redis = FakeRedis()
    # 06:00 slot: only the 06:10 user; the default (07:30) and the 21:50 user are not due
    assert await jobs.ai_dispatch_daily({"redis": redis}, at=at_local(6, 3)) == 1
    assert redis.calls[0][1] == (str(early.id),)
    redis = FakeRedis()
    assert await jobs.ai_dispatch_daily({"redis": redis}, at=at_local(21, 45)) == 1
    assert redis.calls[0][1] == (str(user.id),)
    assert await jobs.ai_dispatch_daily({"redis": FakeRedis()}, at=at_local(12, 0)) == 0
    assert jobs.user_digest_time(models.User(settings={"digest_time": "nonsense"})) == (
        settings.digest_hour_local,
        settings.digest_minute_local,
    )
    assert jobs.weekly_job_id(user.id, "2026-W38") == f"{queue.AI_WEEKLY_REVIEW}:{user.id}:2026-W38"
