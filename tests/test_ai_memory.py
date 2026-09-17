# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.ai import memory, service
from pensieve.ai.service import AIUnavailable
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


async def seed(session, user, n=6):
    feed = make_feed(user, "Feed")
    session.add(feed)
    await session.flush()
    items = [
        make_item(feed, f"Article {i} on topic", f"text {i} kubernetes " * 20, age=timedelta(days=i + 1))
        for i in range(n)
    ]
    session.add_all(items)
    await session.flush()
    return feed, items


async def test_refresh_profile_stores_versions_with_diff(session, user, gateway):
    _feed, items = await seed(session, user)
    batch_time = now() - timedelta(days=1)
    session.add_all(
        [
            make_state(user, items[0], read=True),  # opened
            make_state(user, items[1], starred=True),
            *[
                make_state(user, it, read=True, read_at=batch_time) for it in items[2:6]
            ],  # 4 rows: not a batch yet
        ]
    )
    session.add(
        models.ItemAI(
            user_id=user.id, item_id=items[0].id, tags=["kubernetes"], confidences={"kubernetes": 0.9}
        )
    )
    session.add(models.Note(user_id=user.id, item_id=items[1].id, quote="q", body="remember this"))
    await session.commit()

    gateway.chat({"profile_text": "You follow Kubernetes closely.\nYou star long reads."})
    v1 = await memory.refresh_profile(session, user)
    await session.commit()
    assert v1.version == 1 and v1.body_text.startswith("You follow Kubernetes")
    assert v1.diff_from_previous and "+You follow Kubernetes closely." in v1.diff_from_previous
    body = gateway.chat_calls[0]
    assert body["model"] == settings.llm_long_model and body["reasoning_effort"] == "medium"
    assert gateway.chat_requests[0].headers["X-Workflow"] == "profile"
    prompt = body["messages"][1]["content"]
    assert "kubernetes" in prompt and "remember this" in prompt and '"opened_count": 5' in prompt

    gateway.chat({"profile_text": "You follow Kubernetes closely.\nYou skim release notes."})
    v2 = await memory.refresh_profile(session, user)
    assert v2.version == 2
    assert (
        "-You star long reads." in v2.diff_from_previous
        and "+You skim release notes." in v2.diff_from_previous
    )
    assert (
        "Previous profile:\nYou follow Kubernetes closely." in gateway.chat_calls[1]["messages"][1]["content"]
    )


async def test_mark_all_batch_counts_as_skipped(session, user, gateway):
    _feed, items = await seed(session, user, n=6)
    batch_time = now() - timedelta(days=1)
    session.add_all([make_state(user, it, read=True, read_at=batch_time) for it in items[:5]])
    session.add(make_state(user, items[5], read=True))
    await session.commit()
    signals = await memory.gather_signals(session, user)
    assert signals["skipped_count"] == 5 and signals["opened_count"] == 1


async def test_refresh_profile_respects_memory_toggle(session, user, gateway):
    user.settings = {"memory": False}
    assert await memory.refresh_profile(session, user) is None
    assert gateway.chat_calls == []


async def test_related_history_excludes_same_cluster_and_unread(session, user, gateway):
    _feed, items = await seed(session, user, n=5)
    target, same_cluster, read_close, read_far, unread_close = items
    session.add_all(
        [
            make_embedding(target, 0.0),
            make_embedding(same_cluster, angle_for(0.99)),
            make_embedding(read_close, angle_for(0.9)),
            make_embedding(read_far, angle_for(0.2)),
            make_embedding(unread_close, angle_for(0.95)),
        ]
    )
    session.add_all(
        [
            make_state(user, same_cluster, read=True),
            make_state(user, read_close, read=True),
            make_state(user, read_far, starred=True),
        ]
    )
    c = models.Cluster(
        user_id=user.id, headline="h", window_start=now(), window_end=now(), canonical_item_id=target.id
    )
    session.add(c)
    await session.flush()
    session.add_all(
        [
            models.ClusterItem(cluster_id=c.id, item_id=target.id),
            models.ClusterItem(cluster_id=c.id, item_id=same_cluster.id),
        ]
    )
    session.add(models.Note(user_id=user.id, item_id=read_close.id, quote="", body="my note"))
    await session.commit()

    rows = await service.related_history(session, user, target, limit=5)
    assert [r.item.id for r in rows] == [read_close.id, read_far.id]
    assert rows[0].note is not None and rows[0].note.body == "my note"
    assert abs(rows[0].similarity - 0.9) < 0.01 and rows[1].note is None


async def test_related_history_embeds_on_demand_and_degrades(session, user, gateway):
    feed, items = await seed(session, user, n=2)
    session.add(make_embedding(items[1], 0.0))
    session.add(make_state(user, items[1], read=True))
    await session.commit()
    rows = await service.related_history(session, user, items[0])
    assert len(gateway.embed_calls) == 1 and len(rows) == 1
    # gateway down: read-only helper returns [] rather than raising
    gateway.embeddings(available=True)
    gateway.router.post(f"{settings.llm_base_url.rstrip('/')}/embeddings").mock(
        side_effect=httpx.ConnectError("x")
    )
    fresh = make_item(feed, "New")
    session.add(fresh)
    await session.commit()
    assert await service.related_history(session, user, fresh) == []


async def test_ask_reading_returns_citations(session, user, gateway):
    _feed, items = await seed(session, user, n=3)
    session.add_all([make_embedding(it, angle_for(0.9 - i * 0.1)) for i, it in enumerate(items)])
    session.add_all([make_state(user, it, read=True) for it in items])
    await session.commit()
    gateway.chat({"answer": "Kubernetes came up in [1] and [3].", "citations": [1]})
    answer = await service.ask_reading(session, user, "what did I read about kubernetes")
    assert answer.text.startswith("Kubernetes came up")
    assert len(answer.citations) == 2 and all(isinstance(c, models.Item) for c in answer.citations)
    body = gateway.chat_calls[0]
    assert (
        body["model"] == settings.llm_long_model and gateway.chat_requests[0].headers["X-Workflow"] == "ask"
    )
    assert "[1] Article" in body["messages"][1]["content"]


async def test_ask_reading_fulltext_only_when_embeddings_unavailable(session, user, gateway):
    gateway.embeddings(available=False)
    _feed, _items = await seed(session, user, n=2)
    await session.commit()
    gateway.chat({"answer": "See [1].", "citations": [1]})
    answer = await service.ask_reading(session, user, "kubernetes")
    assert len(answer.citations) == 1


async def test_ask_reading_raises_ai_unavailable(session, user, gateway):
    _feed, _items = await seed(session, user, n=1)
    await session.commit()
    gateway.chat(httpx.Response(500, text="boom"))
    with pytest.raises(AIUnavailable):
        await service.ask_reading(session, user, "kubernetes")


async def test_record_correction(session, user):
    _feed, items = await seed(session, user, n=1)
    await service.record_correction(session, user, "item_tag", items[0].id, "tags", "ai", "databases")
    await session.commit()
    row = await session.scalar(select(models.Correction).where(models.Correction.user_id == user.id))
    assert row.target_type == "item_tag" and row.target_id == items[0].id and row.new_value == "databases"
