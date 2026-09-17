"""httpx client for the LAN LiteLLM gateway (OpenAI-compatible). No SDK, no public API.

Every request carries ``X-Session-ID: pensieve`` and ``X-Workflow: <job kind>`` so the gateway's traces attribute
cost per feature. Node addresses are never hardcoded: everything comes from ``settings``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from pensieve.config import Settings, get_settings

log = logging.getLogger(__name__)

SESSION_ID = "pensieve"
EMBED_BATCH = 32
CHARS_PER_TOKEN = 4


class LLMError(Exception):
    """The gateway failed, returned malformed output, or the output did not match the schema."""


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    def add(self, usage: dict | None) -> None:
        if not usage:
            return
        self.tokens_in += int(usage.get("prompt_tokens") or 0)
        self.tokens_out += int(usage.get("completion_tokens") or 0)


@dataclass
class Health:
    ok: bool
    models: list[str] = field(default_factory=list)
    latency_ms: int = 0


def approx_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Approximate tokens as chars/4 and cut on a line boundary where possible."""
    limit = max(0, max_tokens) * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut + "\n[truncated]"


# ---------------------------------------------------------------------------
# Minimal JSON-schema validation (draft-07 subset: type, properties, required, items, enum,
# additionalProperties, minimum/maximum, minItems/maxItems, anyOf for nullable). No dependency needed.
# ---------------------------------------------------------------------------

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, typ: str) -> bool:
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[typ])


def validate_schema(value: Any, schema: dict, path: str = "$") -> None:
    """Raise ``LLMError`` when ``value`` does not satisfy ``schema``."""
    if "anyOf" in schema:
        errors = []
        for sub in schema["anyOf"]:
            try:
                validate_schema(value, sub, path)
                return
            except LLMError as exc:
                errors.append(str(exc))
        raise LLMError(f"{path}: no anyOf branch matched ({'; '.join(errors)})")
    typ = schema.get("type")
    if typ is not None:
        types = typ if isinstance(typ, list) else [typ]
        if not any(_type_ok(value, t) for t in types):
            raise LLMError(f"{path}: expected {typ}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise LLMError(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise LLMError(f"{path}: missing required key {key!r}")
        for key, sub in props.items():
            if key in value:
                validate_schema(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise LLMError(f"{path}: unexpected keys {sorted(extra)}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise LLMError(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise LLMError(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, element in enumerate(value):
                validate_schema(element, schema["items"], f"{path}[{i}]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise LLMError(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise LLMError(f"{path}: {value} > maximum {schema['maximum']}")


def _extract_json(content: str) -> Any:
    """Parse a JSON object out of model output, tolerating code fences and stray prose."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Thin async client over the gateway. One instance per process is fine; it is stateless apart from usage."""

    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._http = http
        self.usage = Usage()
        """Cumulative token usage since the last ``take_usage()``; jobs mirror it into ``ai_jobs``."""
        self.last_usage: dict = {}
        self.embeddings_available: bool | None = None
        """None = unknown, False once the gateway answered 404/400 on /embeddings (re-probed every call)."""

    # -- plumbing -----------------------------------------------------------------------------------------

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.settings.llm_timeout_s))
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _headers(self, workflow: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
            "X-Session-ID": SESSION_ID,
            "X-Workflow": workflow,
        }

    def take_usage(self) -> tuple[int, int]:
        """Return (tokens_in, tokens_out) accumulated so far and reset the counter."""
        out = (self.usage.tokens_in, self.usage.tokens_out)
        self.usage = Usage()
        return out

    def budget_for(self, model: str) -> int:
        s = self.settings
        return s.llm_max_input_tokens_long if model == s.llm_long_model else s.llm_max_input_tokens_short

    def _fit(self, model: str, system: str, user: str) -> tuple[str, str]:
        """Truncate the user message so system + user fit the model's input budget."""
        budget = self.budget_for(model) - approx_tokens(system) - 64
        return system, truncate_to_tokens(user, max(budget, 256))

    def _body(self, model: str, messages: list[dict], max_tokens: int, **extra: Any) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if model == self.settings.llm_long_model:
            body["reasoning_effort"] = "medium"
        elif model == self.settings.llm_fast_model and self.settings.llm_fast_reasoning_effort:
            body["reasoning_effort"] = self.settings.llm_fast_reasoning_effort
        body.update(extra)
        return body

    async def _post(self, path: str, body: dict, workflow: str) -> dict:
        url = f"{self.settings.llm_base_url.rstrip('/')}{path}"
        try:
            resp = await self.http.post(url, json=body, headers=self._headers(workflow))
        except httpx.HTTPError as exc:
            raise LLMError(f"gateway request failed: {exc!r}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"gateway {path} returned {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError("gateway returned non-JSON body") from exc

    async def _completion(self, body: dict, workflow: str) -> str:
        data = await self._post("/chat/completions", body, workflow)
        usage = data.get("usage") or {}
        self.usage.add(usage)
        self.last_usage = usage
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gateway response has no choices[0].message.content") from exc
        if not isinstance(content, str):
            raise LLMError("gateway returned non-text content")
        return content

    # -- public -------------------------------------------------------------------------------------------

    async def chat_json(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict,
        *,
        max_tokens: int = 1024,
        workflow: str,
        name: str = "response",
    ) -> dict:
        """Structured output. Retries once on malformed output, then once on the long model, then raises."""
        system, user = self._fit(model, system, user)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema, "strict": True},
        }
        attempts = [model, model]
        if model != self.settings.llm_long_model:
            attempts.append(self.settings.llm_long_model)
        last: Exception | None = None
        for attempt_model in attempts:
            body = self._body(attempt_model, messages, max_tokens, response_format=response_format)
            try:
                content = await self._completion(body, workflow)
                parsed = _extract_json(content)
                if not isinstance(parsed, dict):
                    raise LLMError("model returned non-object JSON")
                validate_schema(parsed, schema)
                return parsed
            except (LLMError, ValueError) as exc:
                last = exc
                log.warning("chat_json(%s) attempt on %s failed: %s", workflow, attempt_model, exc)
        raise LLMError(f"chat_json({workflow}) failed after {len(attempts)} attempts: {last}")

    async def chat_text(
        self, model: str, system: str, user: str, *, max_tokens: int = 1024, workflow: str
    ) -> str:
        system, user = self._fit(model, system, user)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return (await self._completion(self._body(model, messages, max_tokens), workflow)).strip()

    async def embed(self, texts: list[str], workflow: str) -> list[list[float]] | None:
        """Embed in batches of 32. Returns None when the gateway does not route embeddings (404/400)."""
        if not texts:
            return []
        out: list[list[float]] = []
        url = f"{self.settings.llm_base_url.rstrip('/')}/embeddings"
        budget = self.settings.llm_max_input_tokens_short
        for start in range(0, len(texts), EMBED_BATCH):
            batch = [truncate_to_tokens(t, budget) or " " for t in texts[start : start + EMBED_BATCH]]
            body = {"model": self.settings.llm_embedding_model, "input": batch}
            try:
                resp = await self.http.post(url, json=body, headers=self._headers(workflow))
            except httpx.HTTPError as exc:
                raise LLMError(f"embeddings request failed: {exc!r}") from exc
            if resp.status_code in (400, 404):
                log.info("embeddings unavailable at gateway (%s)", resp.status_code)
                self.embeddings_available = False
                return None
            if resp.status_code >= 400:
                raise LLMError(f"embeddings returned {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            self.usage.add(data.get("usage"))
            rows = sorted(data.get("data", []), key=lambda r: r.get("index", 0))
            vectors = [list(map(float, r["embedding"])) for r in rows]
            if len(vectors) != len(batch):
                raise LLMError("embeddings response length mismatch")
            dims = self.settings.llm_embedding_dims
            for v in vectors:
                if len(v) != dims:
                    raise LLMError(f"embedding has {len(v)} dims, expected {dims}")
            out.extend(vectors)
        self.embeddings_available = True
        return out

    async def health(self) -> dict:
        """GET the catalog; never raises. ``{"ok": bool, "models": [...], "latency_ms": int}``."""
        started = time.monotonic()
        try:
            resp = await self.http.get(
                self.settings.llm_catalog_url, headers=self._headers("health"), timeout=httpx.Timeout(10.0)
            )
            latency = int((time.monotonic() - started) * 1000)
            if resp.status_code >= 400:
                return {"ok": False, "models": [], "latency_ms": latency}
            return {"ok": True, "models": _catalog_models(resp.json()), "latency_ms": latency}
        except Exception as exc:  # noqa: BLE001 - health must never raise
            log.warning("gateway health check failed: %r", exc)
            return {"ok": False, "models": [], "latency_ms": int((time.monotonic() - started) * 1000)}


def _catalog_models(payload: Any) -> list[str]:
    """Tolerate the common catalog shapes: {"data":[{"id"}]}, {"models":[...]}, [ ... ]."""
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("models") or payload.get("model_list") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    names: list[str] = []
    for row in rows:
        if isinstance(row, str):
            names.append(row)
        elif isinstance(row, dict):
            name = row.get("id") or row.get("model_name") or row.get("name")
            if name:
                names.append(str(name))
    return names


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
