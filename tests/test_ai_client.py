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
from tests.test_ai_helpers import BASE, chat_response, gateway, truncated  # noqa: F401

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


async def test_truncated_json_is_retried_with_more_tokens(gateway):
    gateway.chat(truncated('{"answer": "cut off'), {"answer": "ok", "score": 0.5})
    client = LLMClient()
    out = await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", max_tokens=100)
    assert out["answer"] == "ok"
    assert [c["max_tokens"] for c in gateway.chat_calls] == [100, 200]
    assert [c["model"] for c in gateway.chat_calls] == [settings.llm_fast_model] * 2
    await client.aclose()


async def test_truncation_growth_is_capped_and_text_keeps_partial(gateway):
    gateway.chat(truncated("{bad"), truncated("{bad"), truncated("{bad"))
    client = LLMClient()
    cap = settings.llm_max_output_tokens
    with pytest.raises(LLMError):
        await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x", max_tokens=cap - 10)
    assert [c["max_tokens"] for c in gateway.chat_calls] == [cap - 10, cap, cap]
    gateway.chat(truncated("partial prose"))
    assert await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x") == "partial prose"
    with pytest.raises(LLMTruncated):
        await client._completion({"model": "m", "max_tokens": 1, "messages": []}, "x")
    await client.aclose()


async def test_chat_json_retries_then_falls_back_to_long_model(gateway):
    gateway.chat("not json at all", {"answer": "missing score"}, {"answer": "ok", "score": 0.5})
    client = LLMClient()
    out = await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")
    assert out["answer"] == "ok"
    models_used = [c["model"] for c in gateway.chat_calls]
    assert models_used == [settings.llm_fast_model, settings.llm_fast_model, settings.llm_long_model]
    await client.aclose()


async def test_chat_json_raises_after_all_attempts(gateway):
    gateway.chat("{bad", "{bad", "{bad")
    client = LLMClient()
    with pytest.raises(LLMError):
        await client.chat_json(settings.llm_fast_model, "s", "u", SCHEMA, workflow="x")
    assert len(gateway.chat_calls) == 3
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
