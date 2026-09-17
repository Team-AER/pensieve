# ruff: noqa: F811 -- the `gateway` fixture is imported, then named as a test parameter
import httpx
import pytest

from pensieve.ai.client import LLMClient, LLMError, truncate_to_tokens, validate_schema
from pensieve.config import get_settings
from tests.test_ai_helpers import BASE, gateway  # noqa: F401

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
    assert "reasoning_effort" not in body  # fast model: reasoning off
    assert client.usage.tokens_in == 100 and client.usage.tokens_out == 20
    assert client.take_usage() == (100, 20) and client.usage.tokens_in == 0
    await client.aclose()


async def test_long_model_sends_reasoning_effort(gateway):
    gateway.chat("plain text answer")
    client = LLMClient()
    text = await client.chat_text(settings.llm_long_model, "sys", "user", workflow="digest")
    assert text == "plain text answer"
    assert gateway.chat_calls[0]["reasoning_effort"] == "medium"
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
    assert vectors is not None and len(vectors) == 70 and all(len(v) == 768 for v in vectors)
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


async def test_base_url_from_settings(gateway):
    gateway.chat("ok")
    client = LLMClient()
    await client.chat_text(settings.llm_fast_model, "s", "u", workflow="x")
    assert str(gateway.chat_requests[0].url) == f"{BASE}/chat/completions"
    await client.aclose()
