"""Reader-mode extraction of a linked article via trafilatura. Never raises."""

from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

from pensieve.fetch import http as fetch_http
from pensieve.fetch.parse import sanitize_html

log = logging.getLogger(__name__)

MAX_PAGE_BYTES = 5 * 1024 * 1024


def _extract(html: str, url: str) -> str | None:
    return trafilatura.extract(
        html,
        url=url,
        output_format="html",
        include_images=True,
        include_links=True,
        include_comments=False,
    )


async def extract_reader_html(url: str) -> str | None:
    """Fetch ``url`` and return sanitised reader-mode HTML, or ``None`` on any failure."""
    try:
        response = await fetch_http.get(url)
        if response.status_code != 200:
            log.info("reader mode: %s returned HTTP %s", url, response.status_code)
            return None
        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type and "xml" not in content_type:
            log.info("reader mode: %s is %s, skipping", url, content_type)
            return None
        html = response.content[:MAX_PAGE_BYTES].decode(response.encoding or "utf-8", errors="replace")
        extracted = await asyncio.to_thread(_extract, html, str(response.url))
        if not extracted:
            return None
        cleaned = sanitize_html(extracted, base_url=str(response.url))
        return cleaned or None
    except (httpx.HTTPError, fetch_http.UnsafeURLError) as exc:
        log.info("reader mode: fetch of %s failed: %s", url, exc)
        return None
    except Exception:
        log.exception("reader mode: extraction of %s failed", url)
        return None


__all__ = ["extract_reader_html"]
