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
    fetch_user_agent: str = "Pensieve/0.1 (+https://github.com/Team-AER/pensieve)"
    fetch_timeout_s: float = 20.0
    fetch_per_host_concurrency: int = 2
    item_retention_days: int = 730

    # Fetch hardening, favicons, web security and login rate limiting (fetch/web/syncapi owner block)
    fetch_max_bytes: int = 10 * 1024 * 1024  # abort any feed/page download past this many (decoded) bytes
    fetch_total_timeout_s: float = 60.0  # wall-clock cap per fetch incl. redirects, DNS and body streaming
    favicon_max_bytes: int = 512 * 1024
    favicon_refresh_days: int = 7
    session_cookie_secure: bool = (
        False  # LAN over plain http by default; set true behind TLS (also enables HSTS)
    )
    login_rate_limit_attempts: int = 10  # failed sign-ins per (client ip, email) before a 429
    login_rate_limit_window_s: int = 15 * 60
    undo_batch_max_ids: int = (
        5000  # mark-all-read keeps at most this many ids for Undo (ids live in Redis, not the form)
    )

    # LLM gateway (LiteLLM, OpenAI-compatible, keyless on the LAN). Never a public API.
    llm_base_url: str = "http://llm-proxy.cls/v1"
    llm_api_key: str = "unused"  # gateway overwrites Authorization; SDK still wants a value
    llm_catalog_url: str = "http://llm-proxy.cls/catalog.json"
    llm_fast_model: str = "qwen3.8:27b-iq4_xs"  # tagging, content type, cluster headline (reasoning off)
    llm_long_model: str = "Qwen/Qwen3.8-Flash-Next"  # digest, weekly review, profile, ask my reading
    llm_fast_reasoning_effort: str | None = (
        "none"  # Qwen routes think by default; short structured jobs must not
    )
    llm_embedding_model: str = "nomic-embed-text"
    llm_embedding_dims: int = 768  # fixed by the pgvector column; changing it needs a migration
    llm_timeout_s: float = 240.0  # per request; a 27B on Ollama needs well over a minute for a 5-item batch
    llm_max_input_tokens_short: int = 8_000
    llm_max_input_tokens_long: int = 32_000

    # AI behaviour
    ai_enabled: bool = True
    ai_max_items_per_job: int = (
        40  # newest N per feed job; a first fetch of an archive feed must not flood the gateway
    )
    cluster_window_hours: int = 72
    cluster_similarity_threshold: float = 0.82
    digest_hour_local: int = 7
    digest_minute_local: int = 30
    timezone: str = "Asia/Kolkata"

    # AI package (reasoning, output limits, embeddings probe, title-Jaccard clustering)
    # The LiteLLM proxy validates reasoning_effort against none/minimal/low/medium/high/xhigh/max before it
    # reaches the model, so "off" (Flash-Next's own spelling) is rejected with a 400. `none` passes through.
    llm_long_reasoning_off_value: str = "none"
    # Requests in flight per model, per process, and AI jobs the worker runs at once. Defaults only: the
    # Gateway card (Manage -> AI and memory) overrides all three without a restart. A route that serialises
    # (one GPU, no batching) wants 1; a batching vLLM route can take more.
    llm_fast_concurrency: int = 2
    llm_long_concurrency: int = 2
    ai_max_jobs: int = 2
    llm_max_output_tokens: int = 8192  # ceiling when a truncated (finish_reason=length) JSON call is retried
    llm_embeddings_reprobe_min: int = 30  # after a 400/404 on /embeddings, do not retry for this many minutes
    llm_digest_reasoning: str = (
        "low"  # reasoning_effort for the digest / weekly review / profile (long model)
    )
    cluster_jaccard_merge_threshold: float = (
        0.45  # no-embeddings path: title Jaccard at/above this merges outright
    )
    cluster_jaccard_confirm_threshold: float = 0.30  # ... and this band up to merge asks the LLM to confirm

    # Saved links and the page archive (pensieve/archive). Blobs go to an S3 API (Garage in compose); with no
    # access key set, saving still works but keeps only the extracted text and reader HTML in Postgres.
    s3_endpoint: str = "http://localhost:3900"
    s3_bucket: str = "pensieve-archive"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "garage"
    garage_admin_url: str = "http://localhost:3903"  # only `python -m pensieve.archive init-storage` uses it
    garage_admin_token: str = ""
    garage_capacity_gb: int = 200  # the single node's layout capacity; Garage refuses writes past it
    browser_ws: str = (
        "ws://localhost:3000/"  # Playwright run-server (compose service `browser`); "" = no rendering
    )
    capture_timeout_s: float = 45.0  # page load + settle budget inside the browser
    capture_max_asset_mb: int = 60  # images/fonts/css kept from one capture, in total
    capture_max_file_mb: int = 40  # PDFs and other files saved as-is
    capture_shot_max_px: int = 12000  # full-page screenshot height cap
    capture_concurrency: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
