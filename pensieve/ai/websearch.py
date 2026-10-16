"""Web search for article chat: SearXNG's JSON API finds pages, the SSRF-guarded fetcher reads them.

SearXNG is a service the admin configured (``settings.search_url``, normally the compose service ``search`` on a
private address), so it is called directly. Every result page is untrusted and goes through
``pensieve.fetch.http.get`` like a feed does. Nothing here raises: a failed search or page is simply left out.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
import trafilatura

from pensieve.config import get_settings
from pensieve.fetch import http as fetch_http

log = logging.getLogger(__name__)

PAGE_MAX_BYTES = 3 * 1024 * 1024
SNIPPET_CHARS = 400


@dataclass
class WebResult:
    url: str
    title: str
    snippet: str = ""
    published: str | None = None
    text: str = ""
    """The page's main text once it was read (``read_pages``); empty when only the search snippet is known."""

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").removeprefix("www.")


def available() -> bool:
    return bool(get_settings().search_url)


def canonical(url: str) -> str:
    """Comparable form of a URL: no fragment, no trailing slash, lower-case host."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower().removeprefix("www."), path, parts.query, "")
    )


async def search(query: str, *, limit: int = 5, http: httpx.AsyncClient | None = None) -> list[WebResult]:
    """Top ``limit`` web results for ``query`` (http(s) pages only), or [] when search is off or fails."""
    settings = get_settings()
    if not settings.search_url or not query.strip():
        return []
    url = settings.search_url.rstrip("/") + "/search"
    params = {"q": query.strip(), "format": "json", "safesearch": "0"}
    try:
        if http is not None:
            resp = await http.get(url, params=params, timeout=settings.search_timeout_s)
        else:
            async with httpx.AsyncClient(timeout=settings.search_timeout_s) as own:
                resp = await own.get(url, params=params)
        if resp.status_code != 200:
            log.warning("web search %r: SearXNG returned %s", query, resp.status_code)
            return []
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("web search %r failed: %r", query, exc)
        return []
    rows = (payload.get("results") if isinstance(payload, dict) else None) or []
    out: list[WebResult] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = str(row.get("url") or "")
        if urlsplit(link).scheme not in {"http", "https"}:
            continue
        key = canonical(link)
        if key in seen:
            continue
        seen.add(key)
        snippet = " ".join(str(row.get("content") or "").split())
        out.append(
            WebResult(
                url=link,
                title=" ".join(str(row.get("title") or "").split()) or link,
                snippet=snippet[:SNIPPET_CHARS],
                published=str(row.get("publishedDate") or "")[:10] or None,
            )
        )
    # A site's front page rarely answers anything (Bing likes to return them): keep those after the real pages.
    out.sort(key=lambda r: urlsplit(r.url).path in {"", "/"})
    return out[:limit]


def _extract_text(html: str, url: str) -> str:
    return (
        trafilatura.extract(
            html,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            include_images=False,
        )
        or ""
    )


async def read_page(url: str) -> str:
    """The main text of one page, or "" (refused by the SSRF guard, not HTML, too slow, nothing extracted)."""
    settings = get_settings()
    try:
        async with asyncio.timeout(settings.search_page_timeout_s):
            response = await fetch_http.get(url, max_bytes=PAGE_MAX_BYTES)
            if response.status_code != 200:
                return ""
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "html" not in content_type:
                return ""
            html = response.content.decode(response.encoding or "utf-8", errors="replace")
            return (await asyncio.to_thread(_extract_text, html, str(response.url))).strip()
    except (TimeoutError, httpx.HTTPError, fetch_http.UnsafeURLError) as exc:
        log.info("web search: reading %s failed: %r", url, exc)
        return ""
    except Exception:
        log.exception("web search: extracting %s failed", url)
        return ""


async def read_pages(results: list[WebResult], *, max_chars: int) -> list[WebResult]:
    """Fill ``text`` for every result at once (each capped at ``max_chars``); returns the same list."""
    texts = await asyncio.gather(*(read_page(r.url) for r in results))
    for result, text in zip(results, texts, strict=True):
        result.text = text[:max_chars]
    return results


__all__ = ["WebResult", "available", "canonical", "read_page", "read_pages", "search"]
