"""Admin-chosen gateway models, reasoning effort and concurrency (pensieve.ai.model_choice)."""

import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.ai import model_choice
from pensieve.ai.client import LLMClient
from pensieve.config import get_settings


@pytest.fixture(autouse=True)
def _fresh_choice():
    model_choice.reset_cache()
    yield
    model_choice.reset_cache()


def test_clean_maps_slider_positions_and_defaults():
    out = model_choice.clean(
        {
            "fast": "  m-a ",
            "long": "__default__",
            "embedding": "",
            "fast_reasoning": "3",
            "long_reasoning": "bogus",
        }
    )
    assert out == {"fast": "m-a", "fast_reasoning": "medium"}
    assert model_choice.clean({"fast_reasoning": "99"}) == {"fast_reasoning": "xhigh"}
    assert model_choice.clean({"fast_reasoning": "low"}) == {"fast_reasoning": "low"}
    assert model_choice.ladder_index("high") == 4 and model_choice.ladder_index(None) == 0


async def test_save_applies_and_reloads(session):
    s = get_settings()
    env_fast, env_long = s.llm_fast_model, s.llm_long_model
    defaults = model_choice.env_defaults()
    assert defaults["fast"] == env_fast

    await model_choice.save(session, {"fast": "picked-model", "long_reasoning": "high"})
    await session.commit()
    assert (
        s.llm_fast_model == "picked-model"
        and s.llm_long_model == env_long
        and s.llm_digest_reasoning == "high"
    )
    row = await session.scalar(select(models.AppSetting).where(models.AppSetting.key == "llm"))
    assert row is not None and row.value == {"fast": "picked-model", "long_reasoning": "high"}

    # another process: fresh cache, forced reload from the row
    model_choice.reset_cache()
    assert s.llm_fast_model == env_fast  # reset restored the env default
    got = await model_choice.apply_overrides(session, force=True)
    assert got == {"fast": "picked-model", "long_reasoning": "high"} and s.llm_fast_model == "picked-model"

    # clearing a field falls back to the environment value
    await model_choice.save(session, {})
    await session.commit()
    assert s.llm_fast_model == env_fast and s.llm_digest_reasoning == defaults["long_reasoning"]


async def test_apply_is_cached_and_never_raises(session, monkeypatch):
    calls = {"n": 0}

    async def counting_load(_session):
        calls["n"] += 1
        return {"fast": "x"}

    monkeypatch.setattr(model_choice, "load", counting_load)
    await model_choice.apply_overrides(session, force=True)
    await model_choice.apply_overrides(session)
    await model_choice.apply_overrides(session)
    assert calls["n"] == 1  # within TTL

    async def boom(_session):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_choice, "load", boom)
    got = await model_choice.apply_overrides(session, force=True)
    assert got == {"fast": "x"}  # keeps the last good value


def test_reasoning_and_slots_when_one_model_serves_both_roles(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_fast_model", "same")
    monkeypatch.setattr(s, "llm_long_model", "same")
    monkeypatch.setattr(s, "llm_fast_reasoning_effort", "low")
    monkeypatch.setattr(s, "llm_long_reasoning_off_value", "none")
    monkeypatch.setattr(s, "llm_fast_concurrency", 1)
    monkeypatch.setattr(s, "llm_long_concurrency", 4)
    client = LLMClient()
    # the short-job (fast) effort wins for the shared model, and the larger long-route limit applies
    assert client.reasoning_value("same", None) == "low"
    assert client.reasoning_value("same", "medium") == "medium"
    assert client._slot("same")._value == 4


def test_counts_are_clamped_whole_numbers():
    out = model_choice.clean({"fast_concurrency": "0", "long_concurrency": "99", "ai_jobs": "3"})
    assert out == {"fast_concurrency": 1, "long_concurrency": model_choice.MAX_COUNT, "ai_jobs": 3}
    assert model_choice.clean({"ai_jobs": "two", "fast_concurrency": "-1"}) == {}


async def test_saved_counts_resize_slots_and_the_ai_worker(session, monkeypatch):
    from arq.worker import Worker

    from pensieve.worker import AIWorker, AIWorkerSettings, get_kwargs

    s = get_settings()
    monkeypatch.setattr(s, "llm_fast_model", "fast-m")
    monkeypatch.setattr(s, "llm_long_model", "long-m")
    client = LLMClient()
    assert client._slot("fast-m")._value == 2 and client._slot("long-m")._value == 2  # defaults

    await model_choice.save(session, {"fast_concurrency": 5, "long_concurrency": 1, "ai_jobs": 3})
    assert (s.llm_fast_concurrency, s.llm_long_concurrency, s.ai_max_jobs) == (5, 1, 3)
    assert client._slot("fast-m")._value == 5 and client._slot("long-m")._value == 1

    async def polled(self):
        return None

    monkeypatch.setattr(Worker, "_poll_iteration", polled)
    worker = AIWorker(**(get_kwargs(AIWorkerSettings) | {"max_jobs": model_choice.MAX_COUNT}))
    assert worker.max_jobs == model_choice.MAX_COUNT
    await worker._poll_iteration()
    assert worker.max_jobs == 3  # built for the ceiling, runs what the Gateway card says

    await model_choice.save(session, {})
    await worker._poll_iteration()
    assert worker.max_jobs == 2 and s.llm_fast_concurrency == 2  # cleared: back to the environment
