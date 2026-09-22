# Pensieve

[![CI](https://github.com/Team-AER/pensieve/actions/workflows/ci.yml/badge.svg)](https://github.com/Team-AER/pensieve/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-0f766e.svg)](LICENSE)

Pensieve is a self-hosted RSS reader built for people who want Google Reader speed, Reeder-compatible
sync, and optional local AI without sending their reading history to a hosted model provider.

It provides a responsive web and installable PWA interface, Google Reader and Fever-compatible APIs,
OPML import/export, full-text search, reader mode, rules, folders, tags, and household accounts. Connect
an OpenAI-compatible local gateway to add automatic categorization, story clustering, summaries, reader
memory, and daily insights—or disable AI and use Pensieve as a straightforward feed reader.

## Highlights

- Fast three-pane reader with keyboard shortcuts, mobile navigation, themes, per-account reading settings (text size, font, line height, width, alignment), and offline app shell
- Feed discovery, conditional fetches, reader mode, favicon caching, OPML import/export, and adaptive polling
- Reeder and NetNewsWire support through Google Reader and Fever-compatible sync APIs
- Per-user folders, tags, rules, saved items, notes, search, and household administration
- Optional local-model workflows with configurable models and reasoning effort
- PostgreSQL + pgvector storage, Redis/ARQ jobs, Alembic migrations, and Docker Compose deployment
- SSRF protections, CSRF protection, Argon2 password hashing, rate limiting, and secure proxy headers

## Quick start with Docker Compose

Requirements: Docker Engine with Compose v2 and a hostname served over HTTPS by your reverse proxy.

```bash
git clone https://github.com/Team-AER/pensieve.git
cd pensieve
cp .env.example .env
```

Generate two independent secrets and place them in `.env`:

```bash
openssl rand -hex 32  # PENSIEVE_POSTGRES_PASSWORD
openssl rand -hex 32  # PENSIEVE_SECRET_KEY
```

Set `PENSIEVE_BASE_URL` to the public HTTPS URL, then start the stack:

```bash
docker compose up -d --build
docker compose ps
```

Point your reverse proxy at port `8000`, visit the public URL, and create the first administrator at
`/setup`. Pensieve trusts forwarded headers inside the Compose network, so only expose port 8000 to a
trusted network or proxy. PostgreSQL and Redis are internal-only in the production compose file.

To run without an AI gateway, set:

```dotenv
PENSIEVE_AI_ENABLED=false
```

If you intentionally deploy without HTTPS on a private network, also set
`PENSIEVE_SESSION_COOKIE_SECURE=false`. HTTPS is strongly recommended.

## Configuration

All application settings use the `PENSIEVE_` prefix. The most important values are:

| Variable | Purpose |
| --- | --- |
| `PENSIEVE_POSTGRES_PASSWORD` | Password used by the Compose-managed database; use URL-safe random hex |
| `PENSIEVE_SECRET_KEY` | Signs sessions and action tokens; generate a unique random value |
| `PENSIEVE_BASE_URL` | Canonical externally reachable URL |
| `PENSIEVE_SESSION_COOKIE_SECURE` | Secure cookies and HSTS; defaults to `true` in Compose |
| `PENSIEVE_AI_ENABLED` | Enables or disables all model-backed features |
| `PENSIEVE_LLM_BASE_URL` | Base URL for an OpenAI-compatible model gateway |
| `PENSIEVE_LLM_API_KEY` | Gateway API key, if required |
| `PENSIEVE_LLM_CATALOG_URL` | Optional endpoint used to populate model choices |
| `PENSIEVE_TIMEZONE` | IANA timezone for scheduled insights |

See [.env.example](.env.example) and [`pensieve/config.py`](pensieve/config.py) for the full set of defaults.
Keep `.env` private; it is excluded from Git and the Docker build context.

### Updating

Back up the PostgreSQL volume before an upgrade, then pull and recreate the services. The web container
runs pending Alembic migrations before it accepts traffic.

```bash
git pull --ff-only
docker compose up -d --build
```

## Local development

Pensieve requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker, and Docker Compose.

```bash
cp .env.example .env
# For local-only development, set PENSIEVE_POSTGRES_PASSWORD=pensieve,
# PENSIEVE_BASE_URL=http://localhost:8000, and PENSIEVE_SESSION_COOKIE_SECURE=false.
uv sync --all-extras
make dev-db
make migrate
make dev
```

Run the fetch and AI workers in separate terminals with `make worker` and `make worker-ai`. Quality checks:

```bash
make lint
make test
docker compose build
```

The test suite expects the development PostgreSQL service on `127.0.0.1:5432`; `make dev-db` publishes
database and Redis ports only on loopback through `docker-compose.dev.yaml`.

## Architecture

```text
browser / sync client
        │
        ▼
 FastAPI + Jinja + HTMX ───── PostgreSQL + pgvector
        │                            ▲
        ▼                            │
      Redis ───── fetch worker / optional AI worker ───── model gateway
```

The app is server-rendered and intentionally avoids a Node build pipeline. Static assets, including HTMX,
are vendored so the interface does not depend on a public CDN at runtime.

## Security

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not open a public
issue for an undisclosed security problem.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and review
expectations, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards.

## License

Pensieve is available under the [MIT License](LICENSE).
