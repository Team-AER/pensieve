"""Which gateway models (how much reasoning, how many at once) Pensieve uses, chosen from the UI.

The environment (`PENSIEVE_LLM_*`) supplies defaults; the ``app_settings`` row ``llm`` overrides them
install-wide. Overrides are applied onto the cached ``Settings`` singleton, so every caller that reads
``get_settings().llm_fast_model`` (web and worker alike) sees the choice without a restart. Each process
re-reads the row at most every ``TTL_S`` seconds, or immediately when a job starts or an admin saves.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.config import get_settings

log = logging.getLogger(__name__)

KEY = "llm"
TTL_S = 60.0

# override field -> Settings attribute it shadows
FIELDS: dict[str, str] = {
    "fast": "llm_fast_model",
    "long": "llm_long_model",
    "embedding": "llm_embedding_model",
    "fast_reasoning": "llm_fast_reasoning_effort",  # short structured jobs: tagging, filing, summaries, ask
    "long_reasoning": "llm_digest_reasoning",  # digest, weekly review, profile
    "fast_concurrency": "llm_fast_concurrency",  # requests in flight to the fast model, per process
    "long_concurrency": "llm_long_concurrency",  # requests in flight to the long model, per process
    "ai_jobs": "ai_max_jobs",  # AI jobs the worker runs at once
}
COUNTS = ("fast_concurrency", "long_concurrency", "ai_jobs")
MAX_COUNT = 16
"""Ceiling for every count: the AI worker is built for this many jobs and lowers its limit to the setting."""
# The LiteLLM proxy validates reasoning_effort against exactly these (plus "max"); ordered for the slider.
REASONING_LADDER: list[str] = ["none", "minimal", "low", "medium", "high", "xhigh"]

_env_defaults: dict[str, Any] = {}
_cache: dict[str, Any] = {"loaded_at": 0.0, "values": {}}


def env_defaults() -> dict[str, Any]:
    """The values the environment configured, captured before any override was applied."""
    if not _env_defaults:
        s = get_settings()
        for field, attr in FIELDS.items():
            _env_defaults[field] = getattr(s, attr)
    return dict(_env_defaults)


def effective() -> dict[str, Any]:
    """What is in force right now (env defaults with overrides applied)."""
    s = get_settings()
    return {field: getattr(s, attr) for field, attr in FIELDS.items()}


def overrides() -> dict[str, Any]:
    """The last loaded override row (fields absent = env default)."""
    return dict(_cache["values"])


def _apply(values: dict[str, Any]) -> None:
    s = get_settings()
    defaults = env_defaults()
    for field, attr in FIELDS.items():
        value = values.get(field)
        setattr(s, attr, value if value else defaults[field])
    _cache["values"] = {k: v for k, v in values.items() if v}
    _cache["loaded_at"] = time.monotonic()


async def load(session: AsyncSession) -> dict[str, Any]:
    row = await session.scalar(select(models.AppSetting).where(models.AppSetting.key == KEY))
    return dict(row.value or {}) if row else {}


async def apply_overrides(session: AsyncSession | None = None, *, force: bool = False) -> dict[str, Any]:
    """Refresh the in-process choice from the database (cached ``TTL_S``); never raises."""
    env_defaults()
    if not force and time.monotonic() - _cache["loaded_at"] < TTL_S:
        return overrides()
    try:
        if session is not None:
            values = await load(session)
        else:
            from pensieve.db import session_scope

            async with session_scope() as own:
                values = await load(own)
        _apply(values)
    except Exception as exc:  # noqa: BLE001 - a DB blip must not stop an LLM call; keep what we have
        log.warning("could not load model overrides: %r", exc)
        _cache["loaded_at"] = time.monotonic()
    return overrides()


def clean(form: dict[str, Any]) -> dict[str, str | int]:
    """Validate a form submission into an override dict. Empty = use the env default.

    Reasoning values must be on the ladder; counts are whole numbers from 1 to ``MAX_COUNT``; model ids are
    free text (the catalog may lag the gateway) but are trimmed and capped. Unknown fields are ignored.
    """
    out: dict[str, str | int] = {}
    for field in FIELDS:
        raw = str(form.get(field) or "").strip()[:200]
        if not raw or raw == "__default__":
            continue
        if field in COUNTS:
            if raw.isdigit():
                out[field] = min(max(int(raw), 1), MAX_COUNT)
            continue
        if field.endswith("_reasoning"):
            if raw.isdigit():
                idx = min(int(raw), len(REASONING_LADDER) - 1)
                raw = REASONING_LADDER[idx]
            if raw not in REASONING_LADDER:
                continue
        out[field] = raw
    return out


async def save(session: AsyncSession, values: dict[str, str | int]) -> dict[str, Any]:
    """Persist the override row and apply it to this process immediately."""
    now = datetime.now(UTC)
    stmt = pg_insert(models.AppSetting).values(key=KEY, value=values, updated_at=now)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": values, "updated_at": now})
    await session.execute(stmt)
    _apply(values)
    return overrides()


def ladder_index(value: str | None) -> int:
    """Slider position for a reasoning value (unknown values sit at 'none')."""
    return REASONING_LADDER.index(value) if value in REASONING_LADDER else 0


def reset_cache() -> None:
    """Tests: forget overrides and env defaults."""
    if _env_defaults:
        _apply({})
    _env_defaults.clear()
    _cache.update(loaded_at=0.0, values={})


__all__ = [
    "COUNTS",
    "FIELDS",
    "KEY",
    "MAX_COUNT",
    "REASONING_LADDER",
    "apply_overrides",
    "clean",
    "effective",
    "env_defaults",
    "ladder_index",
    "load",
    "overrides",
    "reset_cache",
    "save",
]
