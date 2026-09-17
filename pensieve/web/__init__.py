"""Web UI (server-rendered Jinja2 + HTMX). Owned by the web package; exposes `router`."""

from fastapi import APIRouter

router = APIRouter()

from pensieve.web import routes  # noqa: F401  (registers routes on `router`)
