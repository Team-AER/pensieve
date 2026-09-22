"""Article extraction for archived pages: trafilatura over the rendered DOM and the server HTML, best one wins.

A server-rendered site gives the same article either way; a client-rendered one (React, Next, a paywall
script that swaps the body) only has text after the browser ran it, and a page that hides text behind a
consent wall sometimes has more in the server HTML. Pick by readable length, preferring the rendered DOM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin

import trafilatura
from lxml import html as lxml_html

from pensieve.fetch.parse import html_to_text, sanitize_html, strip_to_text

MIN_ARTICLE_CHARS = 200  # shorter than this, the "article" is navigation or a cookie notice...
SHORT_PAGE_CHARS = 40  # ...unless neither side found anything longer
TEXT_LIMIT = 400_000  # Postgres tsvector tops out near 1 MB; a book-length page is cut here


@dataclass
class Extracted:
    html: str
    """Sanitised article HTML (images with absolute URLs; the capture rewrites archived ones)."""
    text: str
    title: str | None = None
    byline: str | None = None
    site_name: str | None = None
    published_at: datetime | None = None
    lang: str | None = None
    image: str | None = None
    description: str | None = None
    source: str = "rendered"
    """'rendered' or 'raw': which HTML the article came from."""


def _article(html: str, url: str) -> str | None:
    return trafilatura.extract(
        html,
        url=url,
        output_format="html",
        include_images=True,
        include_links=True,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


_LANG_RE = re.compile(r"<html[^>]*\blang=[\"']?([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)", re.IGNORECASE)


def _lang(html: str) -> str | None:
    match = _LANG_RE.search(html[:4000])
    return match.group(1).lower()[:20] if match else None


def _meta(html: str, url: str) -> dict:
    try:
        doc = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # noqa: BLE001 - metadata is a nicety; a malformed head must not lose the page
        doc = None
    image = getattr(doc, "image", None) if doc else None
    return {
        "title": strip_to_text(getattr(doc, "title", None))[:1000] or None if doc else None,
        "byline": strip_to_text(getattr(doc, "author", None))[:300] or None if doc else None,
        "site_name": strip_to_text(getattr(doc, "sitename", None))[:300] or None if doc else None,
        "published_at": _date(getattr(doc, "date", None)) if doc else None,
        "image": urljoin(url, image) if image else None,
        "description": strip_to_text(getattr(doc, "description", None))[:1000] or None if doc else None,
        "lang": _lang(html),
    }


def _one(html: str | None, url: str, source: str) -> Extracted | None:
    if not html:
        return None
    body = _article(html, url)
    if not body:
        return None
    clean = sanitize_html(body, base_url=url)
    text = html_to_text(clean)[:TEXT_LIMIT]
    return Extracted(html=clean, text=text, source=source, **_meta(html, url))


def extract_best(rendered_html: str | None, raw_html: str | None, url: str) -> Extracted | None:
    """The better of the two extractions, or ``None`` when neither found an article. Sync: run in a thread."""
    rendered = _one(rendered_html, url, "rendered")
    raw = _one(raw_html, url, "raw") if raw_html and raw_html != rendered_html else None
    candidates = [c for c in (rendered, raw) if c and len(c.text) >= MIN_ARTICLE_CHARS]
    if not candidates:
        # A genuinely short page (a landing page, a one-paragraph note) is still better as text than a card.
        candidates = [c for c in (rendered, raw) if c and len(c.text) >= SHORT_PAGE_CHARS]
    if not candidates:
        return None
    # Prefer the rendered DOM unless the server HTML carries clearly more text (a JS wall hid the body).
    best = candidates[0]
    for other in candidates[1:]:
        if len(other.text) > len(best.text) * 1.3:
            best = other
    # Metadata: fill gaps from whichever other side has it.
    for other in candidates:
        if other is best:
            continue
        for name in ("title", "byline", "site_name", "published_at", "lang", "image", "description"):
            if getattr(best, name) is None and getattr(other, name) is not None:
                setattr(best, name, getattr(other, name))
    return best


def page_metadata(html: str | None, url: str) -> dict:
    """Title, site name, image, description for a page with no article (an app, a video page)."""
    return _meta(html, url) if html else {}


def visible_text(html: str | None) -> str:
    """Rough readable text of a whole page, for search when no article was found."""
    if not html:
        return ""
    try:
        tree = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return ""
    for bad in tree.xpath("//script|//style|//noscript|//template|//svg|//nav|//footer|//header"):
        bad.drop_tree()
    text = " ".join(tree.text_content().split())
    return text[:TEXT_LIMIT]


__all__ = ["MIN_ARTICLE_CHARS", "Extracted", "extract_best", "page_metadata", "visible_text"]
