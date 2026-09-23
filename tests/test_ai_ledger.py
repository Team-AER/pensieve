"""The durable AI queue (pensieve.ai.ledger): rows written at enqueue, lost arq jobs re-sent, Redis jobs adopted."""

from datetime import timedelta

from arq.constants import job_key_prefix
from arq.jobs import serialize_job
from sqlalchemy import select

from pensieve import models, queue
from pensieve.ai import ledger
from pensieve.ai import retry as ai_retry
from pensieve.ai.common import utcnow
from tests.test_web_support import login, memory_limiter, seed_feed, seed_item  # noqa: F401


class FakeRedis:
    """Just enough of ArqRedis: a job key per job id and the AI queue's zset."""

    def __init__(self):
        self.jobs: dict[str, bytes] = {}
        self.sent: list[tuple] = []
        self.ttl_ms: dict[str, int] = {}

    async def enqueue_job(self, function, *args, _job_id=None, _queue_name=None, _expires=None):
        if _job_id in self.jobs:
            return None
        self.jobs[_job_id] = serialize_job(function, args, {}, 1, 0)
        self.sent.append((function, args, _job_id, _queue_name, _expires))
        return object()

    async def exists(self, *keys):
        return sum(k.removeprefix(job_key_prefix) in self.jobs for k in keys)

    async def zrange(self, name, start, end):
        return [j.encode() for j in self.jobs]

    async def get(self, key):
        return self.jobs.get(key.removeprefix(job_key_prefix))

    async def pexpire(self, key, ms):
        self.ttl_ms[key.removeprefix(job_key_prefix)] = ms


async def rows(session):
    return (await session.scalars(select(models.AIJob).execution_options(populate_existing=True))).all()


async def test_enqueue_writes_the_row_before_arq_and_sets_no_expiry(session, user):
    feed = await seed_feed(session, user, "Feed")
    items = [await seed_item(session, feed, f"Item {n}") for n in range(3)]
    redis = FakeRedis()
    ids = [str(i.id) for i in items]
    await ledger.enqueue(queue.AI_PROCESS_NEW_ITEMS, str(feed.id), ids, redis=redis)

    (row,) = await rows(session)
    assert (row.kind, row.target_id, row.user_id, row.status) == ("process_items", feed.id, user.id, "queued")
    assert row.function == queue.AI_PROCESS_NEW_ITEMS and row.args == [str(feed.id), ids]
    assert row.id == ledger.row_id_for(row.job_id) and row.job_id.startswith("ai_process_new_items:")
    assert redis.sent == [
        (queue.AI_PROCESS_NEW_ITEMS, (str(feed.id), ids), row.job_id, queue.AI_QUEUE, ledger.NO_EXPIRY)
    ]


async def test_a_waiting_row_is_kept_and_a_finished_one_restarts(session, user):
    redis = FakeRedis()
    await ledger.enqueue(queue.AI_DAILY_DIGEST, str(user.id), job_id="digest:1", redis=redis)
    (row,) = await rows(session)
    first_created = row.created_at
    await ledger.enqueue(queue.AI_DAILY_DIGEST, str(user.id), job_id="digest:1", redis=redis)
    (row,) = await rows(session)
    assert row.created_at == first_created and len(redis.sent) == 1  # arq drops the duplicate as well

    row.status, row.attempts, row.last_error = "done", 1, "note"
    await session.commit()
    redis.jobs.clear()
    await ledger.enqueue(queue.AI_DAILY_DIGEST, str(user.id), job_id="digest:1", redis=redis)
    (row,) = await rows(session)
    assert (row.status, row.attempts, row.last_error) == ("queued", 0, None) and len(redis.sent) == 2


async def test_requeue_lost_resends_only_rows_arq_no_longer_holds(session, user):
    redis = FakeRedis()
    await ledger.enqueue(queue.AI_REFRESH_PROFILE, str(user.id), job_id="profile:held", redis=redis)
    await ledger.enqueue(queue.AI_DAILY_PAPER, str(user.id), None, job_id="paper:lost", redis=redis)
    await ledger.enqueue(queue.AI_WEEKLY_REVIEW, str(user.id), job_id="weekly:fresh", redis=redis)
    for row in await rows(session):
        if row.job_id != "weekly:fresh":
            row.run_after = utcnow() - timedelta(hours=30)
    await session.commit()
    del redis.jobs["paper:lost"], redis.jobs["weekly:fresh"]  # a Redis flush, or arq's own expiry
    redis.sent.clear()

    assert await ledger.requeue_lost(redis) == 1  # the fresh one is inside the grace period
    assert [s[2] for s in redis.sent] == ["paper:lost"] and redis.sent[0][1] == (str(user.id), None)
    # A long wait is normal for a deep backlog: reconcile must not call a ledger row lost.
    assert await ai_retry.reconcile(session, user.id) == 0
    assert {r.status for r in await rows(session)} == {"queued"}


async def test_adopt_gives_redis_jobs_rows_and_the_long_expiry(session, user):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Item")
    redis = FakeRedis()
    await redis.enqueue_job(queue.AI_PROCESS_NEW_ITEMS, str(feed.id), [str(item.id)], _job_id="old:1")
    await redis.enqueue_job(queue.FETCH_FEED, str(feed.id), _job_id="fetch:1")
    legacy = models.AIJob(id=ledger.row_id_for("old:2"), kind="summarize", user_id=user.id, status="queued")
    session.add(legacy)
    await session.commit()
    await redis.enqueue_job(queue.AI_SUMMARIZE_ITEM, str(user.id), str(item.id), "", _job_id="old:2")

    assert await ledger.adopt(redis) == 1
    by_job = {r.job_id: r for r in await rows(session)}
    assert by_job["old:1"].kind == "process_items" and by_job["old:1"].args == [str(feed.id), [str(item.id)]]
    assert by_job["old:2"].status == "queued" and by_job["old:2"].function == queue.AI_SUMMARIZE_ITEM
    assert "fetch:1" not in by_job and set(redis.ttl_ms) == {"old:1", "fetch:1", "old:2"}
    assert await ledger.adopt(redis) == 0  # idempotent


async def test_queue_card_shows_pending_jobs_and_their_articles(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    items = [await seed_item(session, feed, f"Item {n}") for n in range(3)]
    redis = FakeRedis()
    await ledger.enqueue(
        queue.AI_PROCESS_NEW_ITEMS, str(feed.id), [str(i.id) for i in items[:2]], redis=redis
    )
    await ledger.enqueue(queue.AI_SUMMARIZE_ITEMS, str(user.id), [str(items[2].id)], redis=redis)
    await ledger.enqueue(queue.AI_DAILY_DIGEST, str(user.id), redis=redis)
    await login(client, user)

    r = await client.get("/manage/ai")
    assert r.status_code == 200
    card = r.text.split("<h2>Queue</h2>")[1]
    assert '<div class="stat-n">3</div><div class="eyebrow">pending</div>' in card
    assert "3 articles · oldest" in card and "kept in the database until they run" in card
