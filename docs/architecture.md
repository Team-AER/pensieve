# Pensieve architecture

The [README diagram](../README.md#architecture) shows the main data paths. Pensieve is a Python 3.12+
application with server-rendered Jinja templates and HTMX updates. It requires no Node frontend build.

## Services and data

| Service | Responsibility |
| --- | --- |
| `web` | FastAPI web interface and sync/save APIs; runs Alembic migrations before starting |
| `db` | PostgreSQL 17 with pgvector; subscriptions, items, users, reading state and model-derived data |
| `redis` | Redis 7 and ARQ job queues, with append-only persistence |
| `worker` | Feed polling and background feed processing |
| `worker-ai` | Model-assisted categorization, grouping, summaries and insights |
| `worker-capture` | Captures saved pages and writes archive metadata and objects |
| `browser` | Headless Chromium used by the capture worker |
| `garage` | S3-compatible storage for captured HTML, images, screenshots and static copies |
| `storage-init` | One-shot, idempotent Garage layout, key and bucket initialization |

Definitions live in [docker-compose.yaml](../docker-compose.yaml), with host development overrides in
[docker-compose.dev.yaml](../docker-compose.dev.yaml). Worker settings live in
[pensieve/worker.py](../pensieve/worker.py); application configuration lives in
[pensieve/config.py](../pensieve/config.py).

## Deployment boundaries

The production Compose file publishes the web service on port 8000. Serve it behind a trusted HTTPS reverse
proxy and set `PENSIEVE_BASE_URL` to the externally reachable URL. The web process accepts forwarded headers,
so keep direct access restricted to trusted clients/proxies. Database, Redis and Garage have no published
host ports in this file.

The capture worker joins both the application and capture networks. Chromium joins the internal capture
network and a separate egress network, keeping it away from database, Redis and Garage services. Browser
requests pass the application's SSRF checks. Archived pages are served with a restrictive CSP that disables
scripts and limits loads to archive assets.

The development override publishes dependencies on loopback and adds the browser to the default network.
It is for local development; its network topology differs from production.

## Storage and lifecycle

Persistent Compose volumes are `pgdata`, `redisdata`, `garage-meta` and `garage-data`. Preserve `.env` secrets
and back up PostgreSQL and both Garage volumes together so archive references and objects remain consistent.
The web container applies migrations on startup. See [updating](../README.md#updating) and
[archive maintenance](../README.md#saved-links-and-the-page-archive).

AI is optional, but feed fetching and captures need access to their source sites. A gateway URL may point
to local or remote inference; privacy depends on that deployment choice. The configured fast, long and
embedding models must be available at the gateway. Neither the bundled fonts nor HTMX need a runtime CDN.
