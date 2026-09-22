"""Feed autodiscovery: turn any URL a user pastes into a feed URL (plus hub and icon hints)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from lxml import html as lxml_html

from pensieve.fetch import http as fetch_http
from pensieve.fetch.parse import ParsedFeed, ParseError, parse_feed

log = logging.getLogger(__name__)

FEED_LINK_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/feed+json",
        "application/json",
        "application/rdf+xml",
    }
)
WELL_KNOWN_PATHS = ("/feed", "/rss", "/atom.xml", "/feed.xml", "/index.xml", "/rss.xml")
ICON_RELS = ("icon", "shortcut icon", "apple-touch-icon", "apple-touch-icon-precomposed")
MAX_BODY_BYTES = 10 * 1024 * 1024


class DiscoveryError(Exception):
    """No feed could be found at or near the given URL."""


@dataclass(slots=True)
class Discovered:
    feed_url: str
    parsed: ParsedFeed
    etag: str | None = None
    last_modified: str | None = None
    hub: str | None = None
    icon_url: str | None = None
    site_url: str | None = None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _is_html(response: httpx.Response) -> bool:
    ct = response.headers.get("content-type", "").lower()
    if "html" in ct:
        return True
    return response.content.lstrip()[:256].lower().startswith((b"<!doctype html", b"<html"))


def _try_parse(response: httpx.Response) -> ParsedFeed | None:
    if response.status_code != 200 or not response.content:
        return None
    if _is_html(response):
        return None
    try:
        return parse_feed(response.content[:MAX_BODY_BYTES], str(response.url))
    except ParseError:
        return None


@dataclass(slots=True)
class HtmlHints:
    feeds: list[str]
    hub: str | None
    icon: str | None


def extract_html_hints(body: bytes | str, base_url: str) -> HtmlHints:
    """Feed alternates, WebSub hub and icon from an HTML document's ``<link>`` elements."""
    hints = HtmlHints(feeds=[], hub=None, icon=None)
    try:
        doc = lxml_html.fromstring(body)
    except (ValueError, lxml_html.etree.ParserError):
        return hints
    base_el = doc.find(".//base")
    if base_el is not None and base_el.get("href"):
        base_url = urljoin(base_url, base_el.get("href"))
    for link in doc.iter("link"):
        rels = {r.strip().lower() for r in (link.get("rel") or "").split()}
        href = (link.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        ltype = (link.get("type") or "").strip().lower()
        if "alternate" in rels and ltype in FEED_LINK_TYPES and absolute not in hints.feeds:
            hints.feeds.append(absolute)
        elif "hub" in rels and hints.hub is None:
            hints.hub = absolute
        elif hints.icon is None and any(r in rels for r in ("icon", "apple-touch-icon")):
            hints.icon = absolute
        if hints.icon is None and " ".join(sorted(rels)) in ICON_RELS:
            hints.icon = absolute
    return hints


async def _probe_favicon(client: httpx.AsyncClient, page_url: str) -> str | None:
    candidate = _origin(page_url) + "/favicon.ico"
    try:
        response = await fetch_http.get(candidate, client=client)
    except (httpx.HTTPError, fetch_http.UnsafeURLError):
        return None
    if response.status_code == 200 and response.content:
        return str(response.url)
    return None


async def _fetch(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await fetch_http.get(url, client=client)
    except (httpx.HTTPError, fetch_http.UnsafeURLError) as exc:
        log.debug("discovery fetch of %s failed: %s", url, exc)
        return None


def _result(feed_url: str, parsed: ParsedFeed, response: httpx.Response) -> Discovered:
    return Discovered(
        feed_url=feed_url,
        parsed=parsed,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        hub=parsed.hub,
        icon_url=parsed.icon_url,
        site_url=parsed.site_url,
    )


async def discover(url: str, *, client: httpx.AsyncClient | None = None) -> Discovered:
    """Find a feed for ``url``.

    Order: the URL itself; ``<link rel="alternate">`` candidates in its HTML; well-known paths. Also collects
    a WebSub hub and an icon. Raises :class:`DiscoveryError` when nothing parses.
    """
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    await fetch_http.ensure_safe_url(url)

    own_client = client is None
    client = client or fetch_http.get_client()
    try:
        response = await _fetch(client, url)
        if response is None:
            raise DiscoveryError(f"could not fetch {url}")
        if response.status_code >= 400:
            raise DiscoveryError(f"{url} returned HTTP {response.status_code}")

        parsed = _try_parse(response)
        if parsed is not None:
            result = _result(str(response.url), parsed, response)
            if result.icon_url is None and (parsed.site_url or url):
                result.icon_url = await _probe_favicon(client, parsed.site_url or url)
            return result

        page_url = str(response.url)
        hints = (
            extract_html_hints(response.content, page_url)
            if _is_html(response)
            else HtmlHints([], None, None)
        )
        candidates = list(hints.feeds)
        for path in WELL_KNOWN_PATHS:
            candidate = urljoin(page_url, path)
            if candidate not in candidates:
                candidates.append(candidate)
            root_candidate = _origin(page_url) + path
            if root_candidate not in candidates:
                candidates.append(root_candidate)

        for candidate in candidates:
            if candidate == url:
                continue
            resp = await _fetch(client, candidate)
            if resp is None:
                continue
            parsed = _try_parse(resp)
            if parsed is None:
                continue
            result = _result(str(resp.url), parsed, resp)
            result.hub = parsed.hub or hints.hub
            result.site_url = parsed.site_url or page_url
            result.icon_url = parsed.icon_url or hints.icon
            if result.icon_url is None:
                result.icon_url = await _probe_favicon(client, page_url)
            return result
        raise DiscoveryError(f"no feed found at {url}")
    finally:
        if own_client:
            await client.aclose()


__all__ = ["Discovered", "DiscoveryError", "HtmlHints", "discover", "extract_html_hints"]
