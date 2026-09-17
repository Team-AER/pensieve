from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pensieve import models
from pensieve.web import items as items_mod
from tests.test_web_support import HX, fake_module, login, seed_feed, seed_item


async def test_article_marks_read_unless_kept(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Read me", text="Body text here " * 100)
    await login(client, user)
    r = await client.get(f"/items/{item.id}?keep_unread=1", headers=HX)
    assert r.status_code == 200 and "Read me" in r.text and "<html" not in r.text
    assert await session.get(models.ItemState, (user.id, item.id)) is None
    r = await client.get(f"/items/{item.id}", headers=HX)
    assert r.status_code == 200 and "item-state" in r.headers.get("HX-Trigger", "")
    state = await session.get(models.ItemState, (user.id, item.id))
    assert state is not None and state.is_read
    assert "2 min read" in r.text
    # Deep link renders the full reader with the article open.
    r = await client.get(f"/items/{item.id}")
    assert r.status_code == 200 and "<html" in r.text and "Read me" in r.text


async def test_state_endpoints_update_item_states(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Stateful")
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/star", headers=headers | HX)
    assert r.status_code == 200 and "Starred" in r.text
    state = await session.get(models.ItemState, (user.id, item.id))
    assert state.is_starred and state.starred_at is not None and not state.is_read
    r = await client.post(f"/items/{item.id}/read", headers=headers | HX)
    assert r.status_code == 200 and "Mark unread" in r.text
    await session.refresh(state)
    assert state.is_read and state.read_at is not None and state.is_starred
    await client.post(f"/items/{item.id}/unread", headers=headers | HX)
    await client.post(f"/items/{item.id}/unstar", headers=headers | HX)
    await session.refresh(state)
    assert not state.is_read and not state.is_starred
    # CSRF required.
    r = await client.post(f"/items/{item.id}/star", headers=HX)
    assert r.status_code == 403
    r = await client.get("/reader/starred/list", headers=HX)
    assert "All caught up" in r.text


async def test_notes_and_tags(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Annotated")
    session.add(models.ItemAI(user_id=user.id, item_id=item.id, tags=["ai-tag"], confidences={"ai-tag": 0.77}, content_type="tutorial"))
    await session.commit()
    headers = await login(client, user)
    r = await client.get(f"/items/{item.id}", headers=HX)
    assert "ai-tag" in r.text and "77%" in r.text and "tutorial" in r.text
    r = await client.post(f"/items/{item.id}/note", data={"body": "Follow up", "quote": "a line"}, headers=headers | HX)
    assert r.status_code == 200 and "Follow up" in r.text
    note = await session.scalar(select(models.Note).where(models.Note.item_id == item.id))
    assert note.body == "Follow up" and note.quote == "a line" and note.user_id == user.id
    r = await client.post(f"/items/{item.id}/note", data={"body": "Edited", "note_id": str(note.id)}, headers=headers | HX)
    await session.refresh(note)
    assert note.body == "Edited"
    r = await client.post(f"/items/{item.id}/tag", data={"name": "mine", "op": "add"}, headers=headers | HX)
    assert r.status_code == 200 and "mine" in r.text
    state = await session.get(models.ItemState, (user.id, item.id))
    assert state.tags == ["mine"]
    recorded = []

    async def record_correction(session_, user_, target_type, target_id, field, old, new):
        recorded.append((target_type, target_id, field, old, new))

    fake_module(pytest_monkeypatch := __import__("pytest").MonkeyPatch(), "pensieve.ai.service", record_correction=record_correction)
    try:
        r = await client.post(f"/items/{item.id}/tag", data={"name": "ai-tag", "op": "remove"}, headers=headers | HX)
        assert r.status_code == 200 and "ai-tag" not in r.text
    finally:
        pytest_monkeypatch.undo()
    ai = await session.get(models.ItemAI, (user.id, item.id))
    await session.refresh(ai)
    assert ai.tags == [] and recorded == [("item_tag", item.id, "tags", "ai-tag", None)]


async def test_summarize_enqueues_and_polls(client, session, user, monkeypatch):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Summarize me")
    calls = []

    async def enqueue(function, *args, **kwargs):
        calls.append((function, args, kwargs.get("_job_id")))

    monkeypatch.setattr(items_mod.queue, "enqueue", enqueue)
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/summarize", headers=headers | HX)
    assert r.status_code == 200 and "Summarizing" in r.text and f"/items/{item.id}/summary" in r.text
    assert calls == [("ai_summarize_item", (str(user.id), str(item.id)), f"ai_summarize_item:{item.id}")]
    session.add(models.ItemAI(user_id=user.id, item_id=item.id, summary="- One\n- Two"))
    await session.commit()
    r = await client.get(f"/items/{item.id}/summary", headers=HX)
    assert "every 2s" not in r.text and "One" in r.text


async def test_reader_mode_stores_reader_html(client, session, user, monkeypatch):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Extract me", url="https://example.com/article")

    async def extract_reader_html(url):
        assert url == "https://example.com/article"
        return "<p>Clean reader text</p>"

    fake_module(monkeypatch, "pensieve.fetch.reader_mode", extract_reader_html=extract_reader_html)
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/reader-mode", headers=headers | HX)
    assert r.status_code == 200 and "Clean reader text" in r.text and "Show feed version" in r.text
    await session.refresh(item)
    assert item.reader_html == "<p>Clean reader text</p>" and item.reader_fetched_at is not None
    r = await client.post(f"/items/{item.id}/reader-mode", data={"off": "1"}, headers=headers | HX)
    assert "Clean reader text" not in r.text


async def test_unmerge_calls_ai_service(client, session, user, monkeypatch):
    feed = await seed_feed(session, user, "Feed")
    a = await seed_item(session, feed, "A")
    b = await seed_item(session, feed, "B")
    now = datetime.now(UTC)
    cluster = models.Cluster(user_id=user.id, window_start=now - timedelta(days=1), window_end=now, canonical_item_id=a.id, source_count=2)
    session.add(cluster)
    await session.flush()
    session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=a.id), models.ClusterItem(cluster_id=cluster.id, item_id=b.id)])
    await session.commit()
    called = []

    async def unmerge(session_, user_, item_id):
        called.append(item_id)

    fake_module(monkeypatch, "pensieve.ai.service", unmerge=unmerge)
    headers = await login(client, user)
    r = await client.post(f"/items/{b.id}/unmerge", headers=headers | HX)
    assert r.status_code == 200 and called == [b.id]
    assert "list-changed" in r.headers.get("HX-Trigger", "")


async def test_related_history_failures_are_swallowed(client, session, user, monkeypatch):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Robust")

    async def related_history(session_, user_, item_, limit=5):
        raise RuntimeError("gateway down")

    fake_module(monkeypatch, "pensieve.ai.service", related_history=related_history)
    await login(client, user)
    r = await client.get(f"/items/{item.id}", headers=HX)
    assert r.status_code == 200 and "From your memory" not in r.text
