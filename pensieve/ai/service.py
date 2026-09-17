"""Public AI surface for the web and sync layers (see CLAUDE.md). Never lets a gateway failure escape as a 500.

Read-only helpers degrade to empty results; generative calls raise ``AIUnavailable`` which the web layer shows
as a message.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import categorize as _categorize
from pensieve.ai import cluster as _cluster
from pensieve.ai import insights as _insights
from pensieve.ai import memory as _memory
from pensieve.ai.client import LLMError
from pensieve.ai.memory import Answer, RelatedItem

log = logging.getLogger(__name__)


class AIUnavailable(Exception):
    """The LLM gateway is down or returned unusable output; the reader keeps working without AI."""


async def related_history(
    session: AsyncSession, user: models.User, item: models.Item, limit: int = 5
) -> list[RelatedItem]:
    try:
        return await _memory.related_history(session, user, item, limit=limit)
    except (LLMError, httpx.HTTPError) as exc:
        log.warning("related_history unavailable: %s", exc)
        return []


async def ask_reading(session: AsyncSession, user: models.User, question: str) -> Answer:
    """Answer + citations. The Q&A is persisted as an ``insights`` row (kind ``ask``); the web route does not
    commit after asking, so the commit happens here and a failure to commit only logs."""
    try:
        answer = await _memory.ask_reading(session, user, question)
    except (LLMError, httpx.HTTPError) as exc:
        raise AIUnavailable(str(exc)) from exc
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - persisting the transcript is best-effort
        log.warning("could not persist ask transcript: %s", exc)
        await session.rollback()
    return answer


async def summarize_item(session: AsyncSession, user: models.User, item: models.Item) -> str:
    try:
        return await _insights.summarize_item(session, user, item)
    except (LLMError, httpx.HTTPError) as exc:
        raise AIUnavailable(str(exc)) from exc


async def record_correction(
    session: AsyncSession,
    user: models.User,
    target_type: str,
    target_id: uuid.UUID | str,
    field: str,
    old: str | None,
    new: str | None,
) -> None:
    await _memory.record_correction(session, user, target_type, target_id, field, old, new)


async def unmerge(session: AsyncSession, user: models.User, item_id: uuid.UUID | str) -> None:
    await _cluster.unmerge(session, user, uuid.UUID(str(item_id)))


async def dismiss_folder_suggestion(session: AsyncSession, user: models.User, feed: models.Feed) -> None:
    """Clear ``feed``'s AI folder suggestion and record the dismissal as a feed_folder correction. Caller commits."""
    await _categorize.dismiss_folder_suggestion(session, user, feed)


__all__ = [
    "AIUnavailable",
    "Answer",
    "RelatedItem",
    "ask_reading",
    "dismiss_folder_suggestion",
    "record_correction",
    "related_history",
    "summarize_item",
    "unmerge",
]
