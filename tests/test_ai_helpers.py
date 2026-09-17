"""Shared fixtures for the AI package tests: a respx-mocked gateway and seed builders. No test functions here."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from pensieve import models
from pensieve.config import get_settings
from pensieve.models import EMBEDDING_DIMS

settings = get_settings()
BASE = settings.llm_base_url.rstrip("/")
CATALOG = settings.llm_catalog_url


def now() -> datetime:
    return datetime.now(UTC)


def vec(angle: float = 0.0) -> list[float]:
    """Deterministic 768-dim unit vector in the plane of dims 0 and 1: cosine(vec(a), vec(b)) == cos(a - b)."""
    v = [0.0] * EMBEDDING_DIMS
    v[0], v[1] = math.cos(angle), math.sin(angle)
    return v


def angle_for(similarity: float) -> float:
    return math.acos(max(-1.0, min(1.0, similarity)))


def chat_response(payload: dict | str, *, model: str = "mock", tokens: tuple[int, int] = (100, 20)) -> dict:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": sum(tokens)},
    }


def embedding_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    data = []
    for i, text in enumerate(inputs):
        # deterministic vector derived from the text so equal texts embed equally
        h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        data.append({"object": "embedding", "index": i, "embedding": vec((h % 360) * math.pi / 180)})
    return httpx.Response(
        200, json={"object": "list", "data": data, "usage": {"prompt_tokens": 5 * len(inputs)}}
    )


class Gateway:
    """Wrapper over a respx router with helpers for the endpoints the client uses."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.health(ok=True)
        self.embeddings(available=True)

    def health(self, ok: bool = True) -> respx.Route:
        if ok:
            payload = {"data": [{"id": settings.llm_fast_model}, {"id": settings.llm_long_model}]}
            return self.router.get(CATALOG).mock(return_value=httpx.Response(200, json=payload))
        return self.router.get(CATALOG).mock(side_effect=httpx.ConnectError("down"))

    def embeddings(self, available: bool = True) -> respx.Route:
        route = self.router.post(f"{BASE}/embeddings")
        if available:
            return route.mock(side_effect=embedding_response)
        return route.mock(return_value=httpx.Response(404, json={"error": "no route"}))

    def chat(self, *payloads: dict | str | httpx.Response | Exception) -> respx.Route:
        """Queue chat responses in order; the last one repeats. Each payload is a JSON object or raw content."""
        responses = [
            p if isinstance(p, (httpx.Response, Exception)) else httpx.Response(200, json=chat_response(p))
            for p in payloads
        ]

        state = {"i": 0}

        def side_effect(request: httpx.Request) -> httpx.Response:
            i = min(state["i"], len(responses) - 1)
            state["i"] += 1
            r = responses[i]
            if isinstance(r, Exception):
                raise r
            return r

        return self.router.post(f"{BASE}/chat/completions").mock(side_effect=side_effect)

    def chat_by_workflow(self, mapping: dict[str, dict | str]) -> respx.Route:
        """Route chat responses by the X-Workflow header."""

        def side_effect(request: httpx.Request) -> httpx.Response:
            wf = request.headers.get("X-Workflow", "")
            if wf not in mapping:
                return httpx.Response(500, json={"error": f"no mock for workflow {wf}"})
            return httpx.Response(200, json=chat_response(mapping[wf]))

        return self.router.post(f"{BASE}/chat/completions").mock(side_effect=side_effect)

    @property
    def chat_calls(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.chat_requests]

    @property
    def chat_requests(self) -> list[httpx.Request]:
        return [c.request for c in self.router.calls if c.request.url.path.endswith("/chat/completions")]

    @property
    def embed_calls(self) -> list[dict[str, Any]]:
        return [
            json.loads(c.request.content)
            for c in self.router.calls
            if c.request.url.path.endswith("/embeddings")
        ]


@pytest.fixture
def gateway():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield Gateway(router)


# ---------------------------------------------------------------------------
# Seed builders (caller commits)
# ---------------------------------------------------------------------------


def make_feed(
    user: models.User, title: str = "Feed", folder_id: uuid.UUID | None = None, **kw
) -> models.Feed:
    return models.Feed(
        user_id=user.id,
        url=f"https://{uuid.uuid4().hex[:10]}.example/rss",
        title=title,
        folder_id=folder_id,
        **kw,
    )


def item_hash(title: str, text: str) -> str:
    return hashlib.sha256(f"{title.strip().lower()}|{text.strip().lower()}".encode()).hexdigest()


def make_item(
    feed: models.Feed,
    title: str,
    text: str = "",
    *,
    age: timedelta = timedelta(hours=1),
    published_at: datetime | None = None,
    hash_: str | None = None,
) -> models.Item:
    published = published_at or (now() - age)
    return models.Item(
        feed_id=feed.id,
        guid=f"guid-{uuid.uuid4().hex}",
        url=f"https://example.test/{uuid.uuid4().hex[:8]}",
        title=title,
        published_at=published,
        content_html=f"<p>{text}</p>",
        content_text=text,
        hash=hash_ or item_hash(title, text),
    )


def make_embedding(item: models.Item, angle: float = 0.0) -> models.Embedding:
    return models.Embedding(item_id=item.id, vector=vec(angle), model=settings.llm_embedding_model)


def make_state(
    user: models.User,
    item: models.Item,
    *,
    read: bool = False,
    starred: bool = False,
    read_at: datetime | None = None,
    starred_at: datetime | None = None,
) -> models.ItemState:
    return models.ItemState(
        user_id=user.id,
        item_id=item.id,
        is_read=read,
        is_starred=starred,
        read_at=read_at or (now() if read else None),
        starred_at=starred_at or (now() if starred else None),
    )
