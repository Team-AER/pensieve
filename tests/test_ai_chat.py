"""Ask about this article: Markdown rendering, web search, thread numbering, streaming, and the chat routes."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.ai import chat, websearch
from pensieve.ai.client import LLMClient, LLMTruncated
from pensieve.config import get_settings
from pensieve.web import chat as web_chat
from pensieve.web.templating import chat_markdown
from tests.test_ai_helpers import BASE, chat_response, gateway  # noqa: F401
from tests.test_web_support import HX, login, memory_limiter, seed_feed, seed_item  # noqa: F401

SEARCH = "http://search.test"


def sse_body(*chunks: dict | str) -> bytes:
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks]
    return "".join(lines).encode()


def delta(content: str | None = None, reasoning: str | None = None, finish: str | None = None) -> dict:
    d: dict = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}]}


def stream_response(*chunks: dict | str) -> httpx.Response:
    return httpx.Response(
        200, content=sse_body(*chunks, "[DONE]"), headers={"content-type": "text/event-stream"}
    )


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        kind, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            out.append((kind, json.loads(data)))
    return out


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "search_url", SEARCH)


# -- rendering -------------------------------------------------------------------------------------------------


def test_markdown_escapes_and_renders_the_subset():
    sources = {
        1: {"n": 1, "title": "Reuters <story>", "url": "https://reuters.com/a?x=1&y=2", "host": "reuters.com"}
    }
    html = str(
        chat_markdown(
            "Lead **bold** and *soft* with `code` [1].\n\n- one [1, 2]\n- two <script>x</script>\n\n"
            "1. first\n2. second\n\nSee [docs](https://example.com/a_b) or javascript:alert(1) and [x](javascript:alert(1)).",
            sources,
        )
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>bold</strong>" in html and "<em>soft</em>" in html and "<code>code</code>" in html
    assert (
        '<a class="cite" href="https://reuters.com/a?x=1&amp;y=2"' in html and "Reuters &lt;story&gt;" in html
    )
    assert "[1, 2]" in html  # 2 is not a known source: the whole group stays as text
    assert "<ul><li>" in html and "<ol><li>first</li><li>second</li></ol>" in html
    assert '<a href="https://example.com/a_b" target="_blank" rel="noopener noreferrer">docs</a>' in html
    assert 'href="javascript' not in html


def test_markdown_keeps_list_numbering_across_blank_lines_and_code_blocks():
    html = str(chat_markdown("1. a\n\n2. b\n\n```\n<b>raw</b>\n```\n## Heading"))
    assert '<ol start="2"><li>b</li></ol>' in html
    assert "<pre><code>&lt;b&gt;raw&lt;/b&gt;</code></pre>" in html
    assert '<p class="chat-h"><strong>Heading</strong></p>' in html


def test_visible_hides_think_blocks():
    assert chat.visible("<think>plan</think>Answer") == "Answer"
    assert chat.visible("Answer <think>still going") == "Answer "


def test_cited_numbers():
    assert chat.cited_numbers("a [1] b [2, 3] c [x] [12]") == {1, 2, 3, 12}


# -- search and numbering --------------------------------------------------------------------------------------


async def test_search_parses_searxng_dedupes_and_demotes_front_pages(gateway, search_on):  # noqa: F811
    payload = {
        "results": [
            {"url": "https://www.anthropic.com/", "title": "Anthropic", "content": "home"},
            {"url": "https://reuters.com/story#top", "title": "Story", "content": "  the   facts  "},
            {"url": "https://reuters.com/story", "title": "Story again", "content": ""},
            {"url": "ftp://files.example/x", "title": "ftp"},
            {"url": "https://techcrunch.com/x", "title": "TC", "publishedDate": "2026-10-15T10:00:00"},
        ]
    }
    route = gateway.router.get(f"{SEARCH}/search").mock(return_value=httpx.Response(200, json=payload))
    results = await websearch.search("metr anthropic", limit=5)
    assert route.call_count == 1 and route.calls[0].request.url.params["format"] == "json"
    assert [r.url for r in results] == [
        "https://reuters.com/story#top",
        "https://techcrunch.com/x",
        "https://www.anthropic.com/",
    ]
    assert results[0].snippet == "the facts" and results[1].published == "2026-10-15"
    assert results[2].host == "anthropic.com"


async def test_search_never_raises(gateway, search_on, monkeypatch):  # noqa: F811
    gateway.router.get(f"{SEARCH}/search").mock(side_effect=httpx.ConnectError("down"))
    assert await websearch.search("anything") == []
    monkeypatch.setattr(get_settings(), "search_url", "")
    assert await websearch.search("anything") == []


def test_interleave_and_thread_numbers():
    r = websearch.WebResult
    a = [r("https://a.com/1", "a1"), r("https://a.com/2", "a2")]
    b = [r("https://b.com/1", "b1"), r("https://a.com/1/", "dup"), r("https://b.com/2", "b2")]
    merged = chat.interleave([a, b], exclude={websearch.canonical("https://b.com/2")}, limit=10)
    assert [x.title for x in merged] == ["a1", "b1", "a2"]
    earlier = [{"n": 1, "url": "https://b.com/1"}, {"n": 2, "url": "https://old.com"}]
    numbered = chat.number_results(merged, earlier)
    assert [(n.n, n.result.title) for n in numbered] == [(3, "a1"), (1, "b1"), (4, "a2")]


# -- streaming client ------------------------------------------------------------------------------------------


async def test_chat_stream_yields_reasoning_then_content(gateway):  # noqa: F811
    route = gateway.router.post(f"{BASE}/chat/completions").mock(
        return_value=stream_response(
            delta(reasoning="hmm"), delta("Hel"), delta("lo"), {"choices": [], "usage": {"prompt_tokens": 9}}
        )
    )
    client = LLMClient()
    out = [x async for x in client.chat_stream("m", [{"role": "user", "content": "hi"}], workflow="chat")]
    assert out == [("reasoning", "hmm"), ("content", "Hel"), ("content", "lo")]
    body = json.loads(route.calls[0].request.content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert client.usage.tokens_in == 9
    await client.aclose()


async def test_chat_stream_truncated_before_any_answer_raises(gateway):  # noqa: F811
    gateway.router.post(f"{BASE}/chat/completions").mock(
        return_value=stream_response(delta(reasoning="long thought", finish="length"))
    )
    client = LLMClient()
    with pytest.raises(LLMTruncated):
        _ = [x async for x in client.chat_stream("m", [{"role": "user", "content": "hi"}], workflow="chat")]
    await client.aclose()


# -- routes ----------------------------------------------------------------------------------------------------


def gateway_for_chat(gateway, plan: dict, answer: list[str]):  # noqa: F811
    """Plan requests (JSON) and answer requests (stream) on the one chat route."""
    calls: list[dict] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("stream"):
            return stream_response(*(delta(t) for t in answer))
        return httpx.Response(200, json=chat_response(plan))

    gateway.router.post(f"{BASE}/chat/completions").mock(side_effect=side_effect)
    return calls


async def test_ask_streams_an_answer_with_web_sources_and_keeps_the_thread(
    client,
    session,
    user,
    gateway,  # noqa: F811
    search_on,
    monkeypatch,
):
    feed = await seed_feed(session, user, "TechCrunch")
    item = await seed_item(session, feed, "METR gets access", text="Anthropic grants METR access. " * 40)
    calls = gateway_for_chat(
        gateway,
        {"search": True, "queries": ["METR Anthropic access", "METR evaluations"]},
        ["METR can publish ", "findings [1].", "\n\n- More [2]"],
    )
    searched: list[str] = []

    async def fake_search(query, *, limit=5, http=None):
        searched.append(query)
        tag = "a" if "access" in query else "b"
        return [websearch.WebResult(f"https://{tag}.example/story", f"Story {tag}", snippet=f"snippet {tag}")]

    async def fake_read(url):
        return "Full text of " + url

    monkeypatch.setattr(websearch, "search", fake_search)
    monkeypatch.setattr(websearch, "read_page", fake_read)
    headers = await login(client, user)

    r = await client.get(f"/items/{item.id}/chat", headers=HX)
    assert r.status_code == 200 and "Ask about this article" in r.text and "Explain this simply" in r.text
    r = await client.post(
        f"/items/{item.id}/chat", data={"question": "What can METR publish?", "web": "1"}, headers=headers
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and "delta" in kinds and "error" not in kinds
    statuses = [d["text"] for k, d in events if k == "status"]
    assert any("Searching the web" in s for s in statuses) and any("Reading 2 pages" in s for s in statuses)
    assert (
        "".join(d["text"] for k, d in events if k == "delta")
        == "METR can publish findings [1].\n\n- More [2]"
    )
    done = events[-1][1]["html"]
    assert 'class="cite"' in done and "https://a.example/story" in done and "Story b" in done
    assert sorted(searched) == ["METR Anthropic access", "METR evaluations"]

    plan_call, answer_call = calls[0], calls[-1]
    assert plan_call["response_format"]["json_schema"]["name"] == "chat_plan"
    prompt = answer_call["messages"][-1]["content"]
    assert "[1] Story a" in prompt and "Full text of https://a.example/story" in prompt
    assert "QUESTION: What can METR publish?" in prompt
    assert "THE ARTICLE" in answer_call["messages"][0]["content"]

    thread = await session.scalar(
        select(models.ItemChat).where(models.ItemChat.user_id == user.id, models.ItemChat.item_id == item.id)
    )
    await session.refresh(thread)
    assert [m["role"] for m in thread.messages] == ["user", "assistant"]
    assert [s["n"] for s in thread.messages[1]["sources"]] == [1, 2]

    # The panel shows the thread; the article shows the chat on its own when a thread exists.
    r = await client.get(f"/items/{item.id}/chat", headers=HX)
    assert (
        "What can METR publish?" in r.text
        and 'class="cite"' in r.text
        and "Explain this simply" not in r.text
    )
    r = await client.get(f"/items/{item.id}/chat?collapsed=1", headers=HX)
    assert 'class="chat collapsed"' in r.text and "1 question · Show" in r.text
    r = await client.get(f"/items/{item.id}", headers=HX)
    assert f'hx-get="/items/{item.id}/chat?collapsed=1"' in r.text and 'data-action="chat"' in r.text

    # A follow-up numbers a new page after the thread's, and the old sources stay citable.
    async def fake_search_2(query, *, limit=5, http=None):
        return [
            websearch.WebResult("https://a.example/story", "Story a"),
            websearch.WebResult("https://c.example/new", "Story c"),
        ]

    monkeypatch.setattr(websearch, "search", fake_search_2)
    calls.clear()
    r = await client.post(
        f"/items/{item.id}/chat", data={"question": "And since?", "web": "1"}, headers=headers
    )
    assert parse_sse(r.text)[-1][0] == "done"
    prompt = calls[-1]["messages"][-1]["content"]
    assert (
        "[1] Story a" in prompt
        and "[3] Story c" in prompt
        and "[2] Story b https://b.example/story" in prompt
    )
    history = [m["role"] for m in calls[-1]["messages"][1:-1]]
    assert history == ["user", "assistant"]

    r = await client.post(f"/items/{item.id}/chat/clear", headers=headers | HX)
    assert r.status_code == 200 and "Explain this simply" in r.text
    item_id = item.id
    session.expire_all()
    assert await session.scalar(select(models.ItemChat).where(models.ItemChat.item_id == item_id)) is None


async def test_ask_without_web_skips_planning_and_remembers_the_choice(
    client,
    session,
    user,
    gateway,  # noqa: F811
    search_on,
):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Plain", text="Body " * 200)
    calls = gateway_for_chat(gateway, {"search": True, "queries": ["x"]}, ["From the article."])
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/chat", data={"question": "Explain it"}, headers=headers)
    assert parse_sse(r.text)[-1][0] == "done"
    assert len(calls) == 1 and calls[0]["stream"] is True
    assert "Web search is off" in calls[0]["messages"][-1]["content"]
    await session.refresh(user)
    assert user.settings.get("chat_web") is False
    r = await client.get(f"/items/{item.id}/chat", headers=HX)
    assert 'name="web" value="1">' in r.text  # unchecked now


async def test_ask_guards(client, session, user, gateway):  # noqa: F811
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Guarded")
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/chat", data={"question": "hi"})
    assert r.status_code == 403
    r = await client.post(f"/items/{item.id}/chat", data={"question": "  "}, headers=headers)
    assert parse_sse(r.text) == [("error", {"text": "Ask a question first."})]
    user.settings = {"chat": False}
    session.add(user)
    await session.commit()
    r = await client.post(f"/items/{item.id}/chat", data={"question": "hi"}, headers=headers)
    assert parse_sse(r.text)[0][0] == "error" and "turned off" in parse_sse(r.text)[0][1]["text"]
    r = await client.get(f"/items/{item.id}", headers=HX)
    assert 'data-action="chat"' not in r.text


async def test_model_failure_reports_an_error_and_saves_nothing(client, session, user, gateway):  # noqa: F811
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Broken", text="Body " * 200)
    gateway.router.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(503, text="busy"))
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/chat", data={"question": "Why?"}, headers=headers)
    events = parse_sse(r.text)
    assert events[-1][0] == "error" and "503" in events[-1][1]["text"]
    assert await session.get(models.ItemChat, (user.id, item.id)) is None
    await asyncio.sleep(0.01)  # the task's done-callback runs on the next loop pass
    assert not web_chat._RUNNING


def test_paper_story_offers_ask():
    """The paper template gets the button and a slot for the panel (rendered via the story partial)."""
    from pensieve.web.templating import env

    html = env.get_template("partials/paper_story.html").render(
        story={
            "key": "k:1",
            "item_id": "abc",
            "title": "T",
            "members": [],
            "feeds": [],
            "sources": 1,
            "published_at": None,
            "copies": 1,
        },
        edition={"id": "e"},
        config={"show_summaries": False},
        csrf_token="t",
        chat_on=True,
        chatted={"abc"},
    )
    assert 'data-chat-open="abc"' in html and "Continue chat" in html and 'id="chat-slot-abc"' in html
