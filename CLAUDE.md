# Pensieve — engineering contract

Self-hosted RSS reader (Google Reader / Reeder parity) plus a local-LLM layer: categorization, story clustering,
reader memory, insights. All inference goes through the LAN LiteLLM gateway at `http://llm-proxy.cls/v1`
(OpenAI-compatible, keyless). Never call a public LLM API.

PRD: https://claude.ai/code/artifact/02ca0b64-38dc-445c-9240-c1ed678c91a7 · UI design: https://claude.ai/artifact/RL4zZP9yLqZgmMHiphGDUn

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 (async, asyncpg) · Alembic · PostgreSQL 17 + pgvector · Redis + arq ·
Jinja2 + HTMX (server-rendered, no SPA) · feedparser · trafilatura · bleach · httpx. Package manager: uv.

```bash
make dev-db        # docker compose: Postgres (pgvector) on 5432, Redis on 6379
make migrate       # alembic upgrade head
make dev           # uvicorn with reload on :8000
make worker        # arq worker
make test          # pytest (needs dev-db running)
make lint          # ruff
```

## Package ownership (one owner per directory; do not edit another package's files)

| Path | Owner | Contents |
| --- | --- | --- |
| `pensieve/config.py` `db.py` `models.py` `auth.py` `main.py` `worker.py` `queue.py` `templates/base.html` `static/app.css` | foundation (frozen) | Shared contract. Propose changes in your report; do not edit. |
| `pensieve/fetch/` | fetch | Polling, parsing, dedup hash, sanitising, OPML, autodiscovery, reader mode, rules, retention, `jobs.py` |
| `pensieve/web/` + `pensieve/templates/` (except base.html) + `pensieve/static/` (except app.css, app.js, vendor/) | web | Login, reader UI, search, settings/manage pages, keyboard, PWA |
| `pensieve/syncapi/` | syncapi | Google Reader API + Fever API under `/api/...` and `/reader/api/0/...`, token auth |
| `pensieve/ai/` | ai | Gateway client, embeddings, tagging, clustering, profile/memory, digests, `jobs.py` |
| `tests/test_<package>_*.py` | each package | Use your own test DB (below) |
| `alembic/versions/` | foundation | One revision per change set (`842eb4e75246` initial, `b3f1c9a7d2e4` hardening). Need a column? Add it to `models.py` and a new revision in the same change, or say so in your report. |

## Cross-package interfaces (implement exactly these signatures; callers import lazily inside functions)

fetch package exposes:
```python
# pensieve/fetch/subscribe.py
async def add_feed(session, user, url: str, folder_id: uuid.UUID | None = None) -> Feed   # autodiscovers, fetches once, raises FeedError
async def refresh_feed(session, feed: Feed) -> list[Item]                                   # one poll; returns new items
# pensieve/fetch/opml.py
async def import_opml(session, user, xml: bytes) -> ImportResult   # .added .skipped .folders_created
def export_opml(user, feeds: list[Feed], folders: list[Folder]) -> bytes
# pensieve/fetch/reader_mode.py
async def extract_reader_html(url: str) -> str | None
# pensieve/fetch/jobs.py  -> FUNCTIONS = [fetch_feed, fetch_reader_mode, fetch_due_feeds, prune_old_items]; CRON_JOBS schedules fetch_due_feeds every minute and prune nightly
```
After inserting new items, the fetcher enqueues `queue.AI_PROCESS_NEW_ITEMS(feed_id, item_ids)`; on a new feed with no folder it enqueues `queue.AI_FILE_FEED(feed_id)`.

ai package exposes:
```python
# pensieve/ai/service.py
async def related_history(session, user, item, limit=5) -> list[RelatedItem]   # earlier read/starred items by embedding + entities
async def ask_reading(session, user, question: str) -> Answer                  # .text .citations[list[Item]]
async def summarize_item(session, user, item) -> str                           # stores in item_ai.summary, returns markdown
async def record_correction(session, user, target_type, target_id, field, old, new) -> None
async def unmerge(session, user, item_id) -> None                              # split item out of its cluster, store override
# pensieve/ai/jobs.py -> FUNCTIONS = [ai_process_new_items, ai_file_feed, ai_daily_digest, ai_weekly_review, ai_refresh_profile, ai_summarize_item]; CRON_JOBS: digest at settings.digest_hour_local, weekly review Sunday, profile weekly
```
Job names are constants in `pensieve/queue.py`. Web and syncapi read AI results straight from `item_ai`, `clusters`, `cluster_items`, `insights`, `profiles`.

web package exposes nothing; it consumes the above. syncapi exposes nothing.

## Rules everyone follows

- Every query is scoped by `user_id`. No cross-user reads, ever.
- Reader state lives in `item_states`; a missing row means unread and unstarred. Unread count = items without a read state, excluding `hidden`.
- Clusters are per user. A list view "grouped" shows one row per cluster (its canonical item) plus ungrouped items.
- AI is additive: if the gateway is down, the reader must work fully. AI code catches its own exceptions and retries via arq; never let it raise into a web request.
- No secrets in code. Config via `pensieve.config.get_settings()`.
- Sanitise all feed HTML with bleach before storing (`content_html`); never render unsanitised HTML.
- SSRF guard on every outbound fetch: refuse non-http(s), loopback, link-local, and RFC1918 targets unless `settings.debug`.
- Tests: `PENSIEVE_TEST_DATABASE_URL=postgresql+asyncpg://pensieve:pensieve@localhost:5432/pensieve_test_<package> .venv/bin/pytest tests/test_<package>_*.py`. Never hit the network in tests (use respx for httpx).
- Lint clean: `.venv/bin/ruff check pensieve tests`.
- Gateway: send `reasoning_effort: none` (never `off`) when a job must not think; LiteLLM validates the value before the model. Keep `LLMClient` per-model concurrency (`llm_fast_concurrency`/`llm_long_concurrency`); the deployment points both routes at the vLLM model because the Ollama 27B on the 16 GB card takes minutes per request.
- Static assets are versioned through `templating.static()`; bump nothing by hand. Every feed fetch goes through `fetch.http.get` (SSRF guard, DNS pinning, size/time caps).
- Design: use the CSS tokens and classes in `static/app.css`; teal (`--ai`) marks anything AI-generated; no emoji icons, inline stroke SVG only; touch targets ≥ 44px on mobile.
