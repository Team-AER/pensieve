"""Runtime configuration. Every value comes from the environment (prefix PENSIEVE_)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PENSIEVE_", env_file=".env", extra="ignore")

    # Core
    database_url: str = "postgresql+asyncpg://pensieve:pensieve@localhost:5432/pensieve"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = Field(default="change-me-in-production", min_length=8)
    base_url: str = "http://localhost:8000"
    debug: bool = False

    # Fetching
    fetch_default_interval_min: int = 15
    fetch_min_interval_min: int = 5
    fetch_max_interval_min: int = 24 * 60
    fetch_user_agent: str = "Pensieve/0.1 (+https://github.com/prafiles/pensieve)"
    fetch_timeout_s: float = 20.0
    fetch_per_host_concurrency: int = 2
    item_retention_days: int = 730

    # LLM gateway (LiteLLM, OpenAI-compatible, keyless on the LAN). Never a public API.
    llm_base_url: str = "http://llm-proxy.cls/v1"
    llm_api_key: str = "unused"  # gateway overwrites Authorization; SDK still wants a value
    llm_catalog_url: str = "http://llm-proxy.cls/catalog.json"
    llm_fast_model: str = "google/gemma-4-12B-it-qat-w4a16-ct"  # tagging, content type, cluster headline
    llm_long_model: str = "Qwen/Qwen3.8-Flash-Next"  # digest, weekly review, profile, ask my reading
    llm_embedding_model: str = "nomic-embed-text"
    llm_embedding_dims: int = 768  # fixed by the pgvector column; changing it needs a migration
    llm_timeout_s: float = 120.0
    llm_max_input_tokens_short: int = 8_000
    llm_max_input_tokens_long: int = 32_000

    # AI behaviour
    ai_enabled: bool = True
    cluster_window_hours: int = 72
    cluster_similarity_threshold: float = 0.82
    digest_hour_local: int = 7
    digest_minute_local: int = 30
    timezone: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()
