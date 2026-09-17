# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
from sqlalchemy import select

from pensieve import models
from pensieve.ai import categorize, prompts
from pensieve.config import get_settings
from tests.test_ai_helpers import (
    gateway,  # noqa: F401
    make_feed,
    make_item,
)

settings = get_settings()


def tagging_payload(entries: list[tuple[int, list[tuple[str, float]], str]]) -> dict:
    return {
        "items": [
            {"index": i, "tags": [{"name": n, "confidence": c} for n, c in tags], "content_type": ct}
            for i, tags, ct in entries
        ]
    }


async def seed_items(session, user, n=2, feed_title="Feed"):
    feed = make_feed(user, feed_title)
    session.add(feed)
    await session.flush()
    items = [make_item(feed, f"Item {i} about Postgres", f"Body {i} " * 50) for i in range(n)]
    session.add_all(items)
    await session.commit()
    return feed, items


async def test_vocabulary_seeded_once(session, user):
    vocab = await categorize.ensure_vocabulary(session, user)
    assert len(vocab) == len(categorize.DEFAULT_TAGS) >= 40
    assert all(t.kind == "ai" and t.user_id == user.id for t in vocab)
    again = await categorize.ensure_vocabulary(session, user)
    assert len(again) == len(vocab)


async def test_tag_items_writes_item_ai(session, user, gateway):
    _, items = await seed_items(session, user)
    gateway.chat(
        tagging_payload(
            [
                (
                    0,
                    [("databases", 0.95), ("Python", 0.7), ("web", 0.2), ("ai", 0.6), ("nonsense", 0.9)],
                    "tutorial",
                ),
                (1, [("security", 0.55)], "release_note"),
            ]
        )
    )
    rows = await categorize.tag_items(session, user, items)
    await session.commit()
    assert len(rows) == 2
    by_item = {r.item_id: r for r in rows}
    r0 = by_item[items[0].id]
    assert r0.tags == ["databases", "python", "ai"]  # max 3, >= 0.5, vocabulary names, case-normalised
    assert r0.confidences == {"databases": 0.95, "python": 0.7, "ai": 0.6}
    assert r0.content_type == "tutorial"
    assert r0.model == settings.llm_fast_model and r0.prompt_version == prompts.PROMPT_VERSION
    assert by_item[items[1].id].tags == ["security"]
    body = gateway.chat_calls[0]
    assert body["model"] == settings.llm_fast_model and "reasoning_effort" not in body
    assert gateway.chat_requests[0].headers["X-Workflow"] == "tag_items"
    assert "Postgres" in body["messages"][1]["content"]


async def test_tag_items_batches_of_ten(session, user, gateway):
    _, items = await seed_items(session, user, n=12)
    gateway.chat(tagging_payload([(i, [("ai", 0.9)], "article") for i in range(10)]))
    rows = await categorize.tag_items(session, user, items)
    assert len(gateway.chat_calls) == 2
    assert len(rows) == 12  # second batch (2 items) still gets indices 0..1 -> mapped to items 10..11


async def test_tag_items_respects_toggles(session, user, gateway):
    _, items = await seed_items(session, user)
    gateway.chat(tagging_payload([(0, [("ai", 0.9)], "article")]))
    user.settings = {"tag_items": False}
    assert await categorize.tag_items(session, user, items) == []
    user.settings = {}
    user.ai_enabled = False
    assert await categorize.tag_items(session, user, items) == []
    assert gateway.chat_calls == []


async def test_tag_items_uses_corrections_as_few_shot(session, user, gateway):
    _, items = await seed_items(session, user)
    session.add(
        models.Correction(
            user_id=user.id,
            target_type="item_tag",
            target_id=items[0].id,
            field="tags",
            old_value="ai, python",
            new_value="databases",
        )
    )
    await session.commit()
    gateway.chat(
        tagging_payload([(0, [("databases", 0.9)], "article"), (1, [("databases", 0.9)], "article")])
    )
    await categorize.tag_items(session, user, items)
    prompt = gateway.chat_calls[0]["messages"][1]["content"]
    assert "reader chose [databases]" in prompt and "AI said [ai, python]" in prompt


async def test_file_feed_suggests_and_auto_files(session, user, gateway):
    folder = models.Folder(user_id=user.id, name="Databases")
    session.add(folder)
    await session.flush()
    feed, _ = await seed_items(session, user, feed_title="Planet PostgreSQL")
    gateway.chat({"folder": "databases", "new_folder": None, "confidence": 0.95})
    target = await categorize.file_feed(session, user, feed)
    await session.commit()
    assert target.id == folder.id
    assert feed.suggested_folder_id == folder.id and feed.suggested_folder_confidence == 0.95
    assert feed.folder_id == folder.id  # auto-filed at >= 0.9
    assert gateway.chat_requests[0].headers["X-Workflow"] == "file_feed"
    prompt = gateway.chat_calls[0]["messages"][1]["content"]
    assert "Planet PostgreSQL" in prompt and "Databases" in prompt and "Item 0 about Postgres" in prompt


async def test_file_feed_low_confidence_only_suggests(session, user, gateway):
    folder = models.Folder(user_id=user.id, name="Databases")
    session.add(folder)
    await session.flush()
    feed, _ = await seed_items(session, user)
    gateway.chat({"folder": "Databases", "new_folder": None, "confidence": 0.6})
    await categorize.file_feed(session, user, feed)
    assert feed.suggested_folder_id == folder.id and feed.folder_id is None


async def test_file_feed_respects_auto_file_toggle(session, user, gateway):
    folder = models.Folder(user_id=user.id, name="Databases")
    session.add(folder)
    await session.flush()
    feed, _ = await seed_items(session, user)
    user.settings = {"auto_file": False}
    gateway.chat({"folder": "Databases", "new_folder": None, "confidence": 0.99})
    await categorize.file_feed(session, user, feed)
    assert feed.suggested_folder_id == folder.id and feed.folder_id is None


async def test_file_feed_creates_new_folder(session, user, gateway):
    feed, _ = await seed_items(session, user)
    gateway.chat({"folder": None, "new_folder": "Data Engineering", "confidence": 0.7})
    target = await categorize.file_feed(session, user, feed)
    await session.commit()
    created = await session.scalar(select(models.Folder).where(models.Folder.user_id == user.id))
    assert created is not None and created.name == "Data Engineering" and created.ai_suggested is True
    assert target.id == created.id and feed.suggested_folder_id == created.id and feed.folder_id is None
