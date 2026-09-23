# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
import httpx
import pytest

from pensieve.ai.client import (
    LLMClient,
    LLMError,
    LLMTruncated,
    reset_embedding_probe,
    truncate_to_tokens,
    validate_schema,
)
from pensieve.config import get_settings
from tests.test_ai_helpers import BASE, CATALOG, chat_response, gateway, truncated  # noqa: F401

settings = get_settings()
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "score": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": ["answer", "score"],
    "additionalProperties": False,
}


async def test_chat_json_sends_headers_and_schema(gateway):
    gateway.chat({"answer": "yes", "score": 0.9})
    client = LLMClient()
    out = await client.chat_json(
        settings.llm_fast_model, "sys", "user", SCHEMA, workflow="tag_items", name="t"
    )
    assert out == {"answer": "yes", "score": 0.9}
    req = gateway.chat_requests[0]
    assert req.headers["X-Session-ID"] == "pensieve"
    assert req.headers["X-Workflow"] == "tag_items"
    assert req.headers["Authorization"] == f"Bearer {settings.llm_api_key}"
    body = gateway.chat_calls[0]
    assert body["model"] == settings.llm_fast_model
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"] == {"name": "t", "schema": SCHEMA, "strict": True}
    assert (
        body["reasoning_effort"] == "none"
    )  # fast model: Qwen routes think by default, so switch it off explicitly
    assert client.usage.tokens_in == 100 and client.usage.tokens_out == 20
    assert client.take_usage() == (100, 20) and client.usage.tokens_in == 0
    await client.aclose()


async def test_long_model_sends_reasoning_effort(gateway):
    gateway.chat("plain text answer")
    client = LLMClient()
    text = await client.chat_text(
        settings.llm_long_model, "sys", "user", workflow="digest", reasoning="medium"
    )
    assert text == "plain text answer"
    assert (
        gateway.chat_calls[0]["reasoning_effort"] == "medium"
    )  # the caller's explicit level, passed through
    await client.aclose()


async def test_reasoning_effort_is_clamped_to_what_the_catalog_offers(gateway, monkeypatch):
    """A slider level the route does not offer (the vLLM Flash-Next route took only off/low/medium/xhigh one
    day and 400ed every job on "high") is mapped to the nearest offered level once health() saw the catalog."""
    from pensieve.ai import client as client_mod

    monkeypatch.setattr(client_mod, "_EFFORTS", {})
    monkeypatch.setattr(client_mod, "_EFFORT_WARNED", set())
    payload = {
        "models": [
            {"id": settings.llm_fast_model, "reasoning_efforts": ["off", "low", "medium", "xhigh"]},
            {"id": "other-model"},
        ]
    }
    gateway.router.get(settings.llm_catalog_url).mock(return_value=httpx.Response(200, json=payload))
    client = LLMClient()
    health = await client.health()
    assert health["ok"] and health["efforts"] == {settings.llm_fast_model: ["off", "low", "medium", "xhigh"]}
    gateway.chat({"answer": "x", "score": 0.1})
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", reasoning="high")
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", reasoning="minimal")
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", reasoning="off")
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")  # default "none" = off
    await client.chat_json("other-model", "s", "u", SCHEMA, workflow="x", reasoning="high")
    efforts = [c.get("reasoning_effort") for c in gateway.chat_calls]
    assert efforts == [
        "medium",
        "none",
        "none",
        "none",
        "high",
    ]  # minimal sits between none and low; lower wins
    assert client_mod.clamp_effort(settings.llm_fast_model, "max") == "xhigh"
    assert client_mod.clamp_effort("unknown", "high") == "high" and client_mod.clamp_effort("x", None) is None
    await client.aclose()


async def test_reasoning_off_is_spelled_per_model(gateway):
    """Off (the default) is `off` on Flash-Next and `none` on the Ollama route; explicit levels pass through."""
    gateway.chat({"answer": "x", "score": 0.1})
    client = LLMClient()
    await client.chat_json(settings.llm_long_model, "s", "u", SCHEMA, workflow="ask")
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="tag_items")
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", reasoning="low")
    efforts = [c["reasoning_effort"] for c in gateway.chat_calls]
    assert efforts == [settings.llm_long_reasoning_off_value, settings.llm_fast_reasoning_effort, "low"]
    assert efforts[:2] == ["none", "none"]  # both routes: LiteLLM validates against none|low|medium|high
    assert client.reasoning_value("some-other-model", None) is None
    await client.aclose()


async def test_every_request_gets_at_least_the_output_window(gateway):
    gateway.chat({"answer": "ok", "score": 0.5})
    client = LLMClient()
    window = settings.llm_max_output_tokens
    await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", max_tokens=100)
    await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x", max_tokens=window * 2)
    assert [c["max_tokens"] for c in gateway.chat_calls] == [window, window * 2]
    await client.aclose()


async def test_chat_requests_ask_for_at_least_the_catalog_output(gateway):
    """A reasoning model spends output before its answer, so the gateway's published output is the floor."""
    gateway.router.get(CATALOG).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": settings.llm_fast_model, "max_output_tokens": 65_536}]}
        )
    )
    gateway.chat("ok")
    client = LLMClient()
    await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    await client.chat_text("model-the-catalog-does-not-list", "s", "u", workflow="x")
    assert [c["max_tokens"] for c in gateway.chat_calls] == [65_536, settings.llm_max_output_tokens]
    assert len([c for c in gateway.router.calls if c.request.url == CATALOG]) == 1  # read once, then cached
    await client.aclose()


async def test_truncation_surfaces_at_once(gateway):
    gateway.chat(truncated('{"answer": "cut off'))
    client = LLMClient()
    with pytest.raises(LLMTruncated, match="raise the output window"):
        await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")
    assert len(gateway.chat_calls) == 1
    gateway.chat(truncated("partial prose"))
    assert await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x") == "partial prose"
    await client.aclose()


async def test_reasoning_that_fills_the_window_is_reported_not_non_text(gateway):
    body = chat_response("x", finish_reason="length")
    body["choices"][0]["message"]["content"] = None
    gateway.chat(httpx.Response(200, json=body))
    client = LLMClient()
    with pytest.raises(LLMTruncated, match="while still reasoning"):
        await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")
    with pytest.raises(LLMTruncated):
        await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    await client.aclose()


def test_request_timeout_follows_the_models_role(monkeypatch):
    client = LLMClient()
    monkeypatch.setattr(settings, "llm_fast_model", "fast-m")
    monkeypatch.setattr(settings, "llm_long_model", "long-m")
    assert client.timeout_for("fast-m") == settings.llm_fast_timeout_min * 60 == 600
    assert client.timeout_for("long-m") == settings.llm_long_timeout_min * 60 == 1800
    assert client.timeout_for("bge-m3") == 600
    monkeypatch.setattr(settings, "llm_long_model", "fast-m")
    assert client.timeout_for("fast-m") == 1800


async def test_timeout_names_the_model_and_the_setting(gateway):
    gateway.router.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout(""))
    client = LLMClient()
    with pytest.raises(LLMError, match="no answer within 10 min; raise its timeout"):
        await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    await client.aclose()


async def test_chat_json_retries_once_on_the_same_model_then_raises(gateway):
    gateway.chat("not json at all", {"answer": "missing score"}, {"answer": "ok", "score": 0.5})
    client = LLMClient()
    with pytest.raises(LLMError, match=settings.llm_fast_model):
        await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")
    assert [c["model"] for c in gateway.chat_calls] == [settings.llm_fast_model] * 2
    await client.aclose()


async def test_chat_json_tolerates_code_fences(gateway):
    gateway.chat('```json\n{"answer": "fenced", "score": 1}\n```')
    client = LLMClient()
    out = await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")
    assert out["answer"] == "fenced"
    await client.aclose()


async def test_http_error_becomes_llm_error(gateway):
    gateway.chat(httpx.Response(503, text="overloaded"))
    client = LLMClient()
    with pytest.raises(LLMError):
        await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    gateway.chat(httpx.ConnectError("boom"))
    with pytest.raises(LLMError):
        await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    await client.aclose()


async def test_embed_batches_and_dims(gateway):
    client = LLMClient()
    vectors = await client.embed([f"text {i}" for i in range(70)], workflow="embed")
    assert (
        vectors is not None
        and len(vectors) == 70
        and all(len(v) == settings.llm_embedding_dims for v in vectors)
    )
    assert [len(c["input"]) for c in gateway.embed_calls] == [32, 32, 6]
    assert all(c["model"] == settings.llm_embedding_model for c in gateway.embed_calls)
    assert client.embeddings_available is True
    await client.aclose()


async def test_embed_unavailable_returns_none(gateway):
    gateway.embeddings(available=False)
    client = LLMClient()
    assert await client.embed(["a"], workflow="embed") is None
    assert client.embeddings_available is False
    await client.aclose()


async def test_embed_short_circuits_after_400_until_reprobe(gateway):
    gateway.embeddings(available=False)
    client = LLMClient()
    assert await client.embed(["a"], workflow="embed") is None
    assert len(gateway.embed_calls) == 1
    # second call (even from another client instance in the same process) does not hit the network
    other = LLMClient()
    assert await other.embed(["b"], workflow="embed") is None
    assert other.embeddings_available is False and len(gateway.embed_calls) == 1
    # after the cool-down (or a manual reset) it probes again and recovers
    reset_embedding_probe()
    gateway.embeddings(available=True)
    vectors = await other.embed(["c"], workflow="embed")
    assert vectors is not None and len(gateway.embed_calls) == 2 and other.embeddings_available is True
    await client.aclose()
    await other.aclose()


async def test_health(gateway):
    client = LLMClient()
    h = await client.health()
    assert h["ok"] is True and settings.llm_fast_model in h["models"] and isinstance(h["latency_ms"], int)
    gateway.health(ok=False)
    h = await client.health()
    assert h == {"ok": False, "models": [], "latency_ms": h["latency_ms"]}
    await client.aclose()


async def test_input_truncated_to_budget(gateway):
    gateway.chat("ok")
    client = LLMClient()
    huge = "word " * (settings.llm_max_input_tokens_short * 4)
    await client.chat_text(settings.llm_fast_model, "s", huge, workflow="x")
    sent = gateway.chat_calls[0]["messages"][1]["content"]
    assert len(sent) <= settings.llm_max_input_tokens_short * 4 + 20
    assert sent.endswith("[truncated]")
    await client.aclose()


def test_truncate_and_validate():
    assert truncate_to_tokens("abc", 10) == "abc"
    assert truncate_to_tokens("a" * 100, 5).startswith("a" * 20)
    validate_schema({"answer": "x", "score": 0.1}, SCHEMA)
    with pytest.raises(LLMError):
        validate_schema({"answer": "x", "score": 2}, SCHEMA)
    with pytest.raises(LLMError):
        validate_schema({"answer": "x", "score": 0.1, "extra": 1}, SCHEMA)
    validate_schema(None, {"anyOf": [{"type": "string"}, {"type": "null"}]})
    with pytest.raises(LLMError):
        validate_schema("nope", {"type": "string", "enum": ["a", "b"]})


def test_chat_response_helper_marks_finish_reason():
    assert chat_response("x")["choices"][0]["finish_reason"] == "stop"
    assert chat_response("x", finish_reason="length")["choices"][0]["finish_reason"] == "length"


async def test_base_url_from_settings(gateway):
    gateway.chat("ok")
    client = LLMClient()
    await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    assert str(gateway.chat_requests[0].url) == f"{BASE}/chat/completions"
    await client.aclose()
