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
    assert body["model"] == settings.llm_fast_model and body["reasoning_effort"] == "none"
    assert gateway.chat_requests[0].headers["X-Workflow"] == "tag_items"
    assert "Postgres" in body["messages"][1]["content"]


async def test_tag_items_batches_of_five_with_sized_max_tokens(session, user, gateway):
    _, items = await seed_items(session, user, n=12)
    gateway.chat(tagging_payload([(i, [("ai", 0.9)], "article") for i in range(categorize.TAG_BATCH)]))
    rows = await categorize.tag_items(session, user, items)
    assert categorize.TAG_BATCH == 5 and len(gateway.chat_calls) == 3
    assert len(rows) == 12  # last batch (2 items) still gets indices 0..1 -> mapped to items 10..11
    per, head = categorize.TAG_TOKENS_PER_ITEM, categorize.TAG_TOKENS_HEADROOM
    assert [c["max_tokens"] for c in gateway.chat_calls] == [5 * per + head, 5 * per + head, 2 * per + head]
    # title + lead only: the prompt never carries more than TAG_TEXT_CHARS of body per item
    body = gateway.chat_calls[0]["messages"][1]["content"]
    assert categorize.TAG_TEXT_CHARS == 700 and len(body) < 5 * (categorize.TAG_TEXT_CHARS + 200)


async def test_tag_items_skips_already_tagged_unless_forced(session, user, gateway):
    _, items = await seed_items(session, user, n=3)
    session.add(
        models.ItemAI(
            user_id=user.id,
            item_id=items[0].id,
            tags=["ai"],
            confidences={"ai": 0.9},
            prompt_version=prompts.PROMPT_VERSION,
        )
    )
    session.add(models.ItemAI(user_id=user.id, item_id=items[1].id, tags=["ai"], prompt_version="old"))
    session.add(models.ItemAI(user_id=user.id, item_id=items[2].id, summary="- s", prompt_version=prompts.PROMPT_VERSION))
    await session.commit()
    gateway.chat(tagging_payload([(0, [("web", 0.9)], "article"), (1, [("web", 0.9)], "article")]))
    rows = await categorize.tag_items(session, user, items)
    # items[0] is current -> skipped; items[1] is stale -> retagged; items[2] only has a summary -> tagged
    assert {r.item_id for r in rows} == {items[1].id, items[2].id}
    sent = gateway.chat_calls[0]["messages"][1]["content"]
    assert "Item 1 about Postgres" in sent and "Item 2 about Postgres" in sent and "Item 0 about" not in sent
    gateway.chat(tagging_payload([(i, [("web", 0.9)], "article") for i in range(3)]))
    assert len(await categorize.tag_items(session, user, items, force=True)) == 3
    assert await categorize.tag_items(session, user, items) == [] and len(gateway.chat_calls) == 2


async def test_off_list_content_type_and_malformed_entry_do_not_fail_batch(session, user, gateway):
    _, items = await seed_items(session, user, n=3)
    payload = tagging_payload([(0, [("ai", 0.9)], "newsletter"), (2, [("web", 0.8)], "article")])
    payload["items"].insert(1, {"index": 1, "tags": [{"name": "ai", "confidence": "high"}], "content_type": 3})
    gateway.chat(payload)
    rows = await categorize.tag_items(session, user, items)
    by_item = {r.item_id: r for r in rows}
    assert set(by_item) == {items[0].id, items[2].id}  # the malformed entry is skipped, not the batch
    assert by_item[items[0].id].tags == ["ai"] and by_item[items[0].id].content_type is None  # coerced
    assert by_item[items[2].id].content_type == "article"
    assert len(gateway.chat_calls) == 1
    # the wire schema no longer carries an enum, so one stray label cannot reject the response
    schema = gateway.chat_calls[0]["response_format"]["json_schema"]["schema"]
    assert "enum" not in schema["properties"]["items"]["items"]["properties"]["content_type"]
    assert "newsletter" not in prompts.CONTENT_TYPES and "article" in prompts.ITEM_TAGGING_SYSTEM


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
            old_value="apple,ai,mobile",
            new_value="ai,mobile",
        )
    )
    await session.commit()
    gateway.chat(
        tagging_payload([(0, [("databases", 0.9)], "article"), (1, [("databases", 0.9)], "article")])
    )
    await categorize.tag_items(session, user, items)
    prompt = gateway.chat_calls[0]["messages"][1]["content"]
    assert "AI said [apple, ai, mobile] → reader kept [ai, mobile]" in prompt


def test_format_tag_examples_renders_full_lists():
    text = prompts.format_tag_examples([("T", ["apple", "ai", "mobile"], ["ai", "mobile"]), ("U", [], [])])
    assert '"T": AI said [apple, ai, mobile] → reader kept [ai, mobile]' in text
    assert '"U": AI said [none] → reader kept [none]' in text
    assert prompts.format_tag_examples([]) == ""


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


async def test_file_feed_creates_new_folder_only_at_high_confidence(session, user, gateway):
    feed, _ = await seed_items(session, user)
    gateway.chat({"folder": None, "new_folder": "Data Engineering", "confidence": 0.7})
    target = await categorize.file_feed(session, user, feed)
    await session.commit()
    assert target is None
    assert await session.scalar(select(models.Folder).where(models.Folder.user_id == user.id)) is None
    assert feed.suggested_folder_id is None and feed.suggested_folder_confidence == 0.7 and feed.folder_id is None
    gateway.chat({"folder": None, "new_folder": "Data Engineering", "confidence": 0.95})
    target = await categorize.file_feed(session, user, feed)
    await session.commit()
    created = await session.scalar(select(models.Folder).where(models.Folder.user_id == user.id))
    assert created is not None and created.name == "Data Engineering" and created.ai_suggested is True
    assert target.id == created.id and feed.suggested_folder_id == created.id and feed.folder_id == created.id


async def test_file_feed_reuses_a_folder_created_during_the_model_call(session, user, gateway, monkeypatch):
    feed, _ = await seed_items(session, user)
    real_examples = categorize.filing_examples

    async def import_lands_meanwhile(session_, user_, names):
        session.add(models.Folder(user_id=user.id, name="Tech News"))  # e.g. an OPML import committing mid-call
        await session.flush()
        return await real_examples(session_, user_, names)

    monkeypatch.setattr(categorize, "filing_examples", import_lands_meanwhile)
    gateway.chat({"folder": None, "new_folder": "tech news", "confidence": 0.95})
    target = await categorize.file_feed(session, user, feed)
    await session.commit()
    folders = list(await session.scalars(select(models.Folder).where(models.Folder.user_id == user.id)))
    assert [f.name for f in folders] == ["Tech News"]
    assert target.id == folders[0].id and feed.folder_id == folders[0].id


async def test_file_feed_uses_folder_corrections_as_few_shot(session, user, gateway):
    dbs, news = models.Folder(user_id=user.id, name="Databases"), models.Folder(user_id=user.id, name="News")
    session.add_all([dbs, news])
    await session.flush()
    earlier, _ = await seed_items(session, user, feed_title="Planet PostgreSQL")
    session.add(
        models.Correction(
            user_id=user.id,
            target_type="feed_folder",
            target_id=earlier.id,
            field="folder_id",
            old_value=str(news.id),
            new_value=str(dbs.id),
        )
    )
    await session.commit()
    feed, _ = await seed_items(session, user, feed_title="Postgres Weekly")
    gateway.chat({"folder": "Databases", "new_folder": None, "confidence": 0.8})
    await categorize.file_feed(session, user, feed)
    prompt = gateway.chat_calls[0]["messages"][1]["content"]
    assert '"Planet PostgreSQL": AI suggested [News] → reader filed under [Databases]' in prompt


async def test_dismiss_folder_suggestion_clears_and_records(session, user, gateway):
    folder = models.Folder(user_id=user.id, name="Databases")
    session.add(folder)
    await session.flush()
    feed, _ = await seed_items(session, user)
    feed.suggested_folder_id, feed.suggested_folder_confidence = folder.id, 0.6
    await session.commit()
    from pensieve.ai.service import dismiss_folder_suggestion

    await dismiss_folder_suggestion(session, user, feed)
    await session.commit()
    assert feed.suggested_folder_id is None and feed.suggested_folder_confidence is None
    corr = await session.scalar(select(models.Correction).where(models.Correction.user_id == user.id))
    assert corr.target_type == "feed_folder" and corr.old_value == str(folder.id) and corr.new_value is None
    # the dismissal shows up as a few-shot example on the next filing call
    other, _ = await seed_items(session, user, feed_title="Other")
    gateway.chat({"folder": None, "new_folder": None, "confidence": 0.2})
    await categorize.file_feed(session, user, other)
    assert "AI suggested [Databases] → reader dismissed it" in gateway.chat_calls[0]["messages"][1]["content"]
