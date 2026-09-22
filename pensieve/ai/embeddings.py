"""Item embeddings (nomic-embed-text, 768 dims) and pgvector nearest-neighbour queries."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai.client import LLMClient, get_client
from pensieve.ai.common import user_feed_ids, window
from pensieve.config import get_settings

log = logging.getLogger(__name__)

EMBED_TEXT_CHARS = 2000


def embedding_text(item: models.Item) -> str:
    return f"{item.title}\n\n{item.full_text[:EMBED_TEXT_CHARS]}".strip()


async def embed_items(
    session: AsyncSession, items: list[models.Item], client: LLMClient | None = None, workflow: str = "embed"
) -> bool | None:
    """Write ``embeddings`` rows for items that lack one.

    Returns True when embeddings were produced (or nothing was needed), None when the gateway does not route
    embeddings (callers skip embedding-dependent steps).
    """
    if not items:
        return True
    client = client or get_client()
    ids = [i.id for i in items]
    have = set(
        (
            await session.scalars(select(models.Embedding.item_id).where(models.Embedding.item_id.in_(ids)))
        ).all()
    )
    todo = [i for i in items if i.id not in have]
    if not todo:
        return True
    vectors = await client.embed([embedding_text(i) for i in todo], workflow=workflow)
    if vectors is None:
        return None
    model = get_settings().llm_embedding_model
    session.add_all(
        [
            models.Embedding(item_id=item.id, vector=vec, model=model)
            for item, vec in zip(todo, vectors, strict=True)
        ]
    )
    await session.flush()
    return True


async def get_vectors(session: AsyncSession, item_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[float]]:
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(models.Embedding.item_id, models.Embedding.vector).where(
                models.Embedding.item_id.in_(item_ids)
            )
        )
    ).all()
    return {item_id: list(vec) for item_id, vec in rows}


async def nearest(
    session: AsyncSession,
    user: models.User,
    vector: list[float],
    limit: int = 10,
    within_days: int | None = None,
    *,
    exclude_item_ids: set[uuid.UUID] | None = None,
    since: datetime | None = None,
    only_item_ids: list[uuid.UUID] | None = None,
) -> list[tuple[models.Item, float]]:
    """Closest items among the user's feeds by cosine similarity: ``[(item, similarity), ...]``."""
    distance = models.Embedding.vector.cosine_distance(vector)
    stmt = (
        select(models.Item, distance.label("distance"))
        .join(models.Embedding, models.Embedding.item_id == models.Item.id)
        .where(models.Item.feed_id.in_(user_feed_ids(user.id)))
    )
    if within_days is not None:
        stmt = stmt.where(models.Item.published_at >= window(days=within_days))
    if since is not None:
        stmt = stmt.where(models.Item.published_at >= since)
    if exclude_item_ids:
        stmt = stmt.where(models.Item.id.not_in(list(exclude_item_ids)))
    if only_item_ids is not None:
        if not only_item_ids:
            return []
        stmt = stmt.where(models.Item.id.in_(only_item_ids))
    stmt = stmt.order_by(distance).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [(item, 1.0 - float(dist)) for item, dist in rows]
