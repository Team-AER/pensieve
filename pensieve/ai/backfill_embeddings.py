"""Populate item embeddings after enabling or changing the embedding model.

Run with ``python -m pensieve.ai.backfill_embeddings`` inside the app image.
The job commits each small batch and can safely be resumed after interruption.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pensieve import models
from pensieve.ai.client import LLMClient
from pensieve.ai.embeddings import embedding_text
from pensieve.config import get_settings
from pensieve.db import dispose_engine, session_scope

BATCH = 32


async def main() -> None:
    settings = get_settings()
    client = LLMClient()
    done = 0
    try:
        while True:
            async with session_scope() as session:
                items = (
                    await session.scalars(
                        select(models.Item)
                        .outerjoin(models.Embedding, models.Embedding.item_id == models.Item.id)
                        .where(models.Embedding.item_id.is_(None))
                        .order_by(models.Item.id)
                        .limit(BATCH)
                    )
                ).all()
                if not items:
                    break
                vectors = await client.embed(
                    [embedding_text(item) for item in items], workflow="backfill-embed"
                )
                if vectors is None:
                    raise RuntimeError(f"{settings.llm_embedding_model} is unavailable at the gateway")
                values = [
                    {"item_id": item.id, "vector": vector, "model": settings.llm_embedding_model}
                    for item, vector in zip(items, vectors, strict=True)
                ]
                await session.execute(
                    pg_insert(models.Embedding)
                    .values(values)
                    .on_conflict_do_nothing(index_elements=[models.Embedding.item_id])
                )
                done += len(items)
            print(f"embedded={done} model={settings.llm_embedding_model}", flush=True)
    finally:
        await client.aclose()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
