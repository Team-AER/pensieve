"""Jinja2 environment, CSRF tokens and HTMX-aware rendering for the web package."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.auth import current_user
from pensieve.config import get_settings
from pensieve.db import get_session
from pensieve.models import User

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CSRF_MAX_AGE_S = 60 * 60 * 24 * 7


def _static_stamp() -> str:
    """Build stamp for cache busting: a short digest of the shell assets, computed once per process."""
    h = hashlib.sha1()
    for name in ("app.css", "web.css", "fonts.css", "app.js", "reader.js", "sw.js", "manifest.webmanifest"):
        try:
            h.update((STATIC_DIR / name).read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:10]


STATIC_VERSION = _static_stamp()


def static_url(path: str) -> str:
    """``/static/<path>?v=<STATIC_VERSION>`` so the browser and the service worker pick up new builds."""
    return f"/static/{path.lstrip('/')}?v={STATIC_VERSION}"


# ---------------------------------------------------------------------------
# Filters and helpers
# ---------------------------------------------------------------------------


def timeago(value: datetime | None, now: datetime | None = None) -> str:
    """Compact relative time: 'now', '5m', '3h', '2d', or 'Sep 4' beyond a week."""
    if value is None:
        return ""
    if isinstance(value, str):  # stored insight bodies carry ISO strings
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    now = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = now - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d"
    if value.year == now.year:
        return value.strftime("%b %-d")
    return value.strftime("%b %Y")


def reading_time(text: str | None, wpm: int = 220) -> str:
    words = len((text or "").split())
    minutes = max(1, math.ceil(words / wpm)) if words else 0
    return f"{minutes} min read" if minutes else ""


def date_long(value: str | datetime | None) -> str:
    """'Thursday, 18 September' from a period string or datetime."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return value
    return value.strftime("%A, %-d %B")


def sparkline_points(series: list[int | float] | None, width: int = 120, height: int = 32) -> str:
    """Polyline points for an inline SVG sparkline."""
    if not series:
        return ""
    values = [float(v) for v in series]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    step = width / max(1, n - 1)
    pts = []
    for i, v in enumerate(values):
        x = i * step if n > 1 else width / 2
        y = height - 2 - ((v - lo) / span) * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def summary_blocks(markdown: str | None) -> dict[str, Any]:
    """Split ``insights.render_summary`` output into {"bullets": [...], "why": str} for templates."""
    bullets: list[str] = []
    why: list[str] = []
    in_why = False
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("**Why"):
            in_why = True
            continue
        if line.startswith("- ") and not in_why:
            bullets.append(line[2:].strip())
        elif in_why:
            why.append(line)
        else:
            bullets.append(line.lstrip("-* ").strip())
    return {"bullets": bullets, "why": " ".join(why)}


_TAG_RE = re.compile(r"<[^>]+>")


def host(url: str | None) -> str:
    """'theverge.com' from a URL (no www.), for saved links that have no feed title."""
    from urllib.parse import urlsplit

    try:
        name = urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    return name.removeprefix("www.")


def filesize(n: int | None) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def snippet(text: str | None, length: int = 160) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


THEMES = ("auto", "light", "sepia", "dark", "black")


def theme_for(user: User | None) -> str:
    if user is None:
        return "auto"
    value = str((user.settings or {}).get("theme") or "auto")
    return value if value in THEMES else "auto"


_QUERY_NOISE = {"or", "and", "not"}


def highlight(text: str | None, query: str | None) -> Markup:
    """Escape ``text`` and wrap case-insensitive matches of the query's words in ``<mark>``.

    Quotes, ``-exclusions`` and websearch operators are dropped so only real terms light up.
    """
    escaped = escape(text or "")
    tokens: list[str] = []
    for raw in re.findall(r'"([^"]+)"|(\S+)', query or ""):
        term = (raw[0] or raw[1]).strip("\"'")
        if not term or term.startswith("-") or term.lower() in _QUERY_NOISE:
            continue
        tokens.extend(t for t in term.split() if len(t) > 1)
    if not tokens:
        return Markup(escaped)
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted(set(tokens), key=len, reverse=True)), re.IGNORECASE
    )
    marked = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", str(escaped))
    return Markup(marked)


FONT_SIZES = ("s", "m", "l", "xl")
MEASURES = ("narrow", "normal", "wide")
FONT_FAMILIES = ("sans", "serif", "system")
LINE_HEIGHTS = ("tight", "normal", "loose")
TEXT_ALIGNS = ("left", "justify")

# Reading preferences: settings key -> (allowed values, default). Each one is mirrored to a data-* attribute
# on <html> (see READING_PREF_ATTRS) so app.css can drive typography from it and reader.js can flip it live.
READING_PREFS: dict[str, tuple[tuple[str, ...], str]] = {
    "font_size": (FONT_SIZES, "m"),
    "measure": (MEASURES, "normal"),
    "font_family": (FONT_FAMILIES, "sans"),
    "line_height": (LINE_HEIGHTS, "normal"),
    "text_align": (TEXT_ALIGNS, "left"),
}
READING_PREF_ATTRS = {
    "font_size": "font",
    "measure": "measure",
    "font_family": "face",
    "line_height": "leading",
    "text_align": "align",
}


def reading_pref(user: User | None, key: str) -> str:
    """The user's value for one reading preference, or its default when unset or out of range."""
    choices, default = READING_PREFS[key]
    value = str((user.settings or {}).get(key) or default) if user else default
    return value if value in choices else default


def reading_prefs_for(user: User | None) -> dict[str, str]:
    return {key: reading_pref(user, key) for key in READING_PREFS}


def font_size_for(user: User | None) -> str:
    return reading_pref(user, "font_size")


def measure_for(user: User | None) -> str:
    return reading_pref(user, "measure")


def _csrf_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="csrf")


def make_csrf(subject: uuid.UUID | str | None) -> str:
    return _csrf_serializer().dumps(str(subject or "anon"))


def check_csrf(token: str | None, subject: uuid.UUID | str | None) -> bool:
    if not token:
        return False
    try:
        return _csrf_serializer().loads(token, max_age=CSRF_MAX_AGE_S) == str(subject or "anon")
    except (BadSignature, ValueError):
        return False


CurrentUser = Annotated[User, Depends(current_user)]
DB = Annotated[AsyncSession, Depends(get_session)]


async def csrf_user(request: Request, user: CurrentUser) -> User:
    """POST dependency: an authenticated user whose request carries a valid CSRF token.

    HTMX sends it as the X-CSRF-Token header (inherited hx-headers); plain forms post a csrf_token field.
    """
    token = request.headers.get("x-csrf-token")
    if not token:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get("csrf_token")
            token = value if isinstance(value, str) else None
    if not check_csrf(token, user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    return user


CsrfUser = Annotated[User, Depends(csrf_user)]


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"], default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["timeago"] = timeago
    env.filters["reading_time"] = reading_time
    env.filters["date_long"] = date_long
    env.filters["snippet"] = snippet
    env.filters["host"] = host
    env.filters["filesize"] = filesize
    env.filters["highlight"] = highlight
    env.filters["summary_blocks"] = summary_blocks
    env.filters["tojson_attr"] = lambda v: json.dumps(v)
    env.globals["sparkline_points"] = sparkline_points
    env.globals["now"] = lambda: datetime.now(UTC)
    env.globals["static"] = static_url
    env.globals["static_version"] = STATIC_VERSION
    return env


env = _build_env()


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true" and request.headers.get("hx-boosted") != "true"


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    partial: str | None = None,
    user: User | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render `template` as a full page, or `partial` when the request comes from HTMX (non-boosted)."""
    ctx: dict[str, Any] = {
        "request": request,
        "user": user,
        "theme": theme_for(user),
        **reading_prefs_for(user),
        "reading_pref_attrs": READING_PREF_ATTRS,
        "csrf_token": make_csrf(user.id if user else None),
        "settings": get_settings(),
        "htmx": is_htmx(request),
        "uid": str(user.id) if user else "",
    }
    if context:
        ctx.update(context)
    name = partial if (partial and is_htmx(request)) else template
    html = env.get_template(name).render(ctx)
    return HTMLResponse(html, status_code=status_code, headers=headers)


def hx_trigger(*events: str, **payload: Any) -> dict[str, str]:
    """Build an HX-Trigger header value: bare event names plus optional {event: detail} payloads."""
    data: dict[str, Any] = {e: {} for e in events}
    data.update(payload)
    return {"HX-Trigger": json.dumps(data)}
