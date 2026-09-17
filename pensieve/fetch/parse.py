"""Feed parsing (RSS 1.0/2.0, Atom, JSON Feed) into a normalised shape, plus HTML sanitising and hashing.

XML formats go through feedparser; JSON Feed is handled by hand. Every entry ends up as a
:class:`ParsedEntry` with timezone-aware UTC timestamps, sanitised ``content_html``, plain ``content_text`` and a
``hash`` used for exact-duplicate detection across feeds.
"""

from __future__ import annotations

import calendar
import hashlib
import html as html_lib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
from bleach.html5lib_shim import Filter
from bleach.sanitizer import Cleaner
from dateutil import parser as dateparser

log = logging.getLogger(__name__)

MAX_TITLE = 1000
MAX_AUTHOR = 300
MAX_URL = 2048
MAX_GUID = 2048
SUMMARY_CHARS = 500


class ParseError(ValueError):
    """The body is not a feed we understand."""


@dataclass(slots=True)
class ParsedEntry:
    guid: str
    url: str | None
    title: str
    author: str | None
    published_at: datetime
    content_html: str
    summary: str
    enclosure_url: str | None = None
    enclosure_type: str | None = None
    content_text: str = ""
    hash: str = ""


@dataclass(slots=True)
class ParsedFeed:
    title: str
    site_url: str | None
    description: str
    icon_url: str | None
    hub: str | None
    entries: list[ParsedEntry] = field(default_factory=list)
    kind: str = ""
    """feedparser version string (``rss20``, ``atom10`` ...) or ``json`` for JSON Feed."""


# --------------------------------------------------------------------------------------------------------------
# HTML sanitising
# --------------------------------------------------------------------------------------------------------------

ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "audio", "b", "blockquote", "br", "caption", "cite", "code", "col", "colgroup", "dd",
        "del", "details", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
        "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p", "picture", "pre", "q", "s", "samp", "small",
        "source", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
        "time", "tr", "u", "ul", "var", "video", "iframe",
    }
)  # fmt: skip

ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "rel", "target"],
    "abbr": ["title"],
    "audio": ["src", "controls", "preload"],
    "blockquote": ["cite"],
    "col": ["span"],
    "colgroup": ["span"],
    "details": ["open"],
    "iframe": ["src", "width", "height", "title", "allowfullscreen", "loading"],
    "img": ["src", "srcset", "sizes", "alt", "title", "width", "height", "loading"],
    "ol": ["start", "reversed"],
    "q": ["cite"],
    "source": ["src", "srcset", "sizes", "type", "media"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan", "scope"],
    "time": ["datetime"],
    "video": ["src", "poster", "controls", "preload", "width", "height"],
}

ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto", "data"})
URL_ATTRIBUTES = frozenset({"href", "src", "poster", "cite"})

#: Embeds are only kept from these hosts (and their subdomains), always sandboxed.
IFRAME_HOSTS = frozenset({"youtube.com", "youtube-nocookie.com", "player.vimeo.com"})
IFRAME_SANDBOX = "allow-scripts allow-same-origin allow-popups allow-presentation"


def _host_allowed(host: str | None, allowed: frozenset[str]) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in allowed)


def iframe_src_allowed(src: str | None) -> bool:
    """``https://`` embeds from :data:`IFRAME_HOSTS` only."""
    if not src:
        return False
    try:
        parts = urlsplit(src.strip())
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and _host_allowed(parts.hostname, IFRAME_HOSTS)


def _clean_url_value(value: str, base_url: str | None, *, allow_data_image: bool) -> str | None:
    """Apply the URL rules used for ``src``/``href`` to one URL; ``None`` means drop it."""
    value = (value or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("data:"):
        return value if allow_data_image and lowered.startswith("data:image/") else None
    scheme = urlsplit(value).scheme.lower() if "://" in value or ":" in value.split("/", 1)[0] else ""
    if scheme and scheme not in ("http", "https"):
        return None
    if base_url and not scheme:
        return urljoin(base_url, value)
    return value


def _srcset_candidates(value: str) -> list[tuple[str, str]]:
    """Split ``srcset`` per the HTML algorithm: a URL runs to whitespace; a trailing comma ends a candidate.

    Commas inside URLs (``data:image/png;base64,...``) therefore stay with the URL.
    """
    tokens = value.split()
    out: list[tuple[str, str]] = []
    pending: list[str] = []
    i = 0
    while i < len(tokens) or pending:
        if pending:
            url = pending.pop(0)
        else:
            url = tokens[i]
            i += 1
        if url.endswith(","):
            out.append((url.rstrip(","), ""))
            continue
        descriptor = ""
        if i < len(tokens):
            nxt = tokens[i]
            i += 1
            # A comma ends the descriptor; anything after it starts the next candidate ("1x,https://...").
            descriptor, _, rest = nxt.partition(",")
            if rest:
                pending.append(rest)
        out.append((url, descriptor))
    return [(u, d) for u, d in out if u]


def clean_srcset(value: str | None, base_url: str | None = None, *, allow_data_image: bool = False) -> str:
    """Filter each ``srcset`` candidate's URL like ``src``; keeps descriptors (``2x``, ``640w``)."""
    if not value:
        return ""
    kept: list[str] = []
    for raw_url, descriptor in _srcset_candidates(value):
        url = _clean_url_value(raw_url, base_url, allow_data_image=allow_data_image)
        if url is None:
            continue
        kept.append(f"{url} {descriptor}".strip())
    return ", ".join(kept)


class _UrlAndLinkFilter(Filter):
    """Runs after bleach's sanitiser: absolutises URLs, restricts ``data:`` to images, hardens ``<a>``,
    validates ``srcset`` candidates and keeps ``<iframe>`` only for allow-listed hosts (sandboxed)."""

    def __init__(self, source: Any, base_url: str | None = None) -> None:
        super().__init__(source)
        self.base_url = base_url

    def __iter__(self):
        dropping_iframe = False
        for token in super().__iter__():
            kind = token["type"]
            name = token.get("name")
            if dropping_iframe:
                if kind == "EndTag" and name == "iframe":
                    dropping_iframe = False
                continue
            if kind in ("StartTag", "EmptyTag"):
                if name == "iframe":
                    if not self._fix_iframe(token):
                        dropping_iframe = kind == "StartTag"
                        continue
                    yield token
                    continue
                self._fix(token)
            yield token

    def _fix_iframe(self, token: dict) -> bool:
        data: dict = token["data"]
        src = None
        for key in list(data):
            attr = key[1] if isinstance(key, tuple) else key
            if attr == "src":
                src = (data[key] or "").strip()
        if not iframe_src_allowed(src):
            return False
        data[(None, "sandbox")] = IFRAME_SANDBOX
        data[(None, "referrerpolicy")] = "strict-origin-when-cross-origin"
        data[(None, "loading")] = "lazy"
        return True

    def _fix(self, token: dict) -> None:
        data: dict = token["data"]
        for key in list(data):
            attr = key[1] if isinstance(key, tuple) else key
            if attr == "srcset":
                cleaned = clean_srcset(data[key], self.base_url, allow_data_image=token["name"] == "img")
                if cleaned:
                    data[key] = cleaned
                else:
                    del data[key]
                continue
            if attr not in URL_ATTRIBUTES:
                continue
            value = (data[key] or "").strip()
            lowered = value.lower()
            if lowered.startswith("data:"):
                if not (token["name"] == "img" and attr == "src" and lowered.startswith("data:image/")):
                    del data[key]
                continue
            if self.base_url and value and not urlsplit(value).scheme:
                data[key] = urljoin(self.base_url, value)
        if token["name"] == "a":
            data[(None, "rel")] = "noopener noreferrer nofollow"
            data[(None, "target")] = "_blank"


def _cleaner(base_url: str | None) -> Cleaner:
    return Cleaner(
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
        filters=[partial(_UrlAndLinkFilter, base_url=base_url)],
    )


_SCRIPT_STYLE_RE = re.compile(r"<(script|style|object|embed|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
#: An iframe element, closed or not (an unclosed one swallows nothing: only the open tag is matched then).
_IFRAME_RE = re.compile(r"<iframe\b([^>]*)>(?:.*?</iframe\s*>)?", re.IGNORECASE | re.DOTALL)
_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)


def _prestrip_iframe(match: re.Match) -> str:
    """Drop iframes from unknown hosts before bleach; normalise allowed ones to an empty element."""
    attrs = match.group(1)
    found = _SRC_ATTR_RE.search(attrs)
    src = next((g for g in found.groups() if g is not None), "") if found else ""
    if not iframe_src_allowed(html_lib.unescape(src)):
        return ""
    return f"<iframe{attrs}></iframe>"


def sanitize_html(raw: str | None, base_url: str | None = None) -> str:
    """Bleach-sanitise feed HTML. Unknown tags are stripped (their text kept); script/style bodies are removed.

    ``<iframe>`` survives only for :data:`IFRAME_HOSTS` (with a sandbox); ``<IFRAME`` in any case, closed or
    not, from any other host is removed before bleach so nothing of it leaks through tag stripping.
    """
    if not raw:
        return ""
    # Remove the bodies of script-like elements before bleach so their text does not survive tag stripping.
    cleaned = _SCRIPT_STYLE_RE.sub("", raw)
    cleaned = _IFRAME_RE.sub(_prestrip_iframe, cleaned)
    return _cleaner(base_url).clean(cleaned).strip()


_BLOCK_BREAK_RE = re.compile(
    r"<\s*(?:br|hr)\s*/?>|</\s*(?:p|div|li|h[1-6]|tr|blockquote|pre|dd|dt|figcaption|section|article|table)\s*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v ]+")
_NL_RE = re.compile(r"\s*\n\s*")


def html_to_text(html: str | None) -> str:
    """Plain text of (already sanitised) HTML: block boundaries become newlines, entities are decoded."""
    if not html:
        return ""
    text = _SCRIPT_STYLE_RE.sub("", html)
    text = _IFRAME_RE.sub("", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n", text)
    return text.strip()


def strip_to_text(raw: str | None) -> str:
    """Text for single-line fields (titles, authors): drop all markup and collapse whitespace."""
    return re.sub(r"\s+", " ", html_to_text(sanitize_html(raw))).strip()


def normalise_for_hash(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def content_hash(title: str, content_text: str) -> str:
    """SHA-256 of normalised title + content text: stable across feeds carrying the same article."""
    payload = normalise_for_hash(title) + "\n" + normalise_for_hash(content_text)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


def _absolute(url: str | None, base: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if base and not urlsplit(url).scheme:
        url = urljoin(base, url)
    return _truncate(url, MAX_URL)


def _struct_to_datetime(value: time.struct_time | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    except (OverflowError, ValueError, OSError):
        return None


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601/RFC-822 date string to aware UTC; ``None`` when unparseable."""
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _fallback_guid(title: str, published_at: datetime) -> str:
    return hashlib.sha256(f"{title}\n{published_at.isoformat()}".encode()).hexdigest()


def _finalise(entry: ParsedEntry) -> ParsedEntry:
    entry.title = _truncate(entry.title, MAX_TITLE) or ""
    entry.author = _truncate(entry.author, MAX_AUTHOR)
    entry.guid = _truncate(entry.guid, MAX_GUID) or ""
    entry.content_text = html_to_text(entry.content_html)
    if not entry.summary:
        entry.summary = entry.content_text[:SUMMARY_CHARS]
    entry.hash = content_hash(entry.title, entry.content_text)
    return entry


# --------------------------------------------------------------------------------------------------------------
# JSON Feed
# --------------------------------------------------------------------------------------------------------------


def _looks_like_json(body: bytes) -> bool:
    return body.lstrip()[:1] in (b"{",)


def _json_author(obj: dict) -> str | None:
    authors = obj.get("authors")
    if isinstance(authors, list) and authors:
        names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
        if names:
            return ", ".join(names)
    author = obj.get("author")
    if isinstance(author, dict) and author.get("name"):
        return str(author["name"])
    return None


def parse_json_feed(data: dict, url: str | None, now: datetime) -> ParsedFeed:
    version = str(data.get("version", ""))
    if not version.startswith("https://jsonfeed.org/version/"):
        raise ParseError("not a JSON Feed document")
    site_url = _absolute(data.get("home_page_url"), url)
    feed_author = _json_author(data)
    feed = ParsedFeed(
        title=strip_to_text(str(data.get("title") or "")),
        site_url=site_url,
        description=strip_to_text(str(data.get("description") or "")),
        icon_url=_absolute(data.get("icon") or data.get("favicon"), site_url or url),
        hub=None,
        kind="json",
    )
    hubs = data.get("hubs")
    if isinstance(hubs, list):
        for hub in hubs:
            if isinstance(hub, dict) and hub.get("url"):
                feed.hub = _absolute(str(hub["url"]), url)
                break

    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        link = _absolute(raw.get("url") or raw.get("external_url"), site_url or url)
        title = strip_to_text(str(raw.get("title") or ""))
        published = parse_datetime(raw.get("date_published")) or parse_datetime(raw.get("date_modified")) or now
        if raw.get("content_html"):
            content_html = sanitize_html(str(raw["content_html"]), base_url=link or site_url)
        elif raw.get("content_text"):
            content_html = "<p>" + html_lib.escape(str(raw["content_text"])).replace("\n", "<br>") + "</p>"
        else:
            content_html = ""
        summary = strip_to_text(str(raw.get("summary") or ""))[:SUMMARY_CHARS]
        guid = str(raw.get("id") or "").strip() or link or _fallback_guid(title, published)
        enclosure_url = enclosure_type = None
        attachments = raw.get("attachments")
        if isinstance(attachments, list):
            for att in attachments:
                if isinstance(att, dict) and att.get("url"):
                    enclosure_url = _absolute(str(att["url"]), link or url)
                    enclosure_type = _truncate(str(att.get("mime_type") or "") or None, 100)
                    break
        feed.entries.append(
            _finalise(
                ParsedEntry(
                    guid=guid,
                    url=link,
                    title=title,
                    author=_json_author(raw) or feed_author,
                    published_at=published,
                    content_html=content_html,
                    summary=summary,
                    enclosure_url=enclosure_url,
                    enclosure_type=enclosure_type,
                )
            )
        )
    return feed


# --------------------------------------------------------------------------------------------------------------
# XML (feedparser)
# --------------------------------------------------------------------------------------------------------------


def _best_content(entry: Any) -> str:
    contents = entry.get("content") or []
    html_candidates = [c for c in contents if "html" in (c.get("type") or "")]
    chosen = html_candidates[0] if html_candidates else (contents[0] if contents else None)
    if chosen is not None and chosen.get("value"):
        value = chosen["value"]
        if "html" not in (chosen.get("type") or ""):
            value = "<p>" + html_lib.escape(value).replace("\n", "<br>") + "</p>"
        return value
    return entry.get("summary") or ""


def _entry_summary(entry: Any, content_html: str) -> str:
    summary = entry.get("summary") or ""
    if summary and summary != content_html:
        return strip_to_text(summary)[:SUMMARY_CHARS]
    return ""


def _feed_icon(parsed: Any, base: str | None) -> str | None:
    feed = parsed.feed
    for key in ("icon", "logo"):
        if feed.get(key):
            return _absolute(feed[key], base)
    image = feed.get("image")
    if isinstance(image, dict) and image.get("href"):
        return _absolute(image["href"], base)
    return None


def _feed_hub(parsed: Any, base: str | None) -> str | None:
    for link in parsed.feed.get("links") or []:
        if (link.get("rel") or "").lower() == "hub" and link.get("href"):
            return _absolute(link["href"], base)
    return None


def _author_name(obj: Any) -> str | None:
    """Prefer the parsed name over RSS's ``email (Name)`` convention."""
    detail = obj.get("author_detail")
    if isinstance(detail, dict) and detail.get("name"):
        return str(detail["name"])
    return obj.get("author") or None


def _entry_link(entry: Any, base: str | None) -> str | None:
    if entry.get("link"):
        return _absolute(entry["link"], base)
    for link in entry.get("links") or []:
        if (link.get("rel") or "alternate") == "alternate" and link.get("href"):
            return _absolute(link["href"], base)
    return None


def _entry_enclosure(entry: Any, base: str | None) -> tuple[str | None, str | None]:
    for enc in entry.get("enclosures") or []:
        if enc.get("href"):
            return _absolute(enc["href"], base), _truncate(enc.get("type") or None, 100)
    for media in entry.get("media_content") or []:
        if media.get("url"):
            return _absolute(media["url"], base), _truncate(media.get("type") or None, 100)
    return None, None


def parse_xml_feed(body: bytes, url: str | None, now: datetime) -> ParsedFeed:
    parsed = feedparser.parse(body, response_headers={"content-location": url} if url else None)
    if not parsed.get("version") and not parsed.entries and not parsed.feed.get("title"):
        raise ParseError("document is not a recognised RSS/Atom feed")

    site_url = _absolute(parsed.feed.get("link"), url)
    base = site_url or url
    feed = ParsedFeed(
        title=strip_to_text(parsed.feed.get("title") or ""),
        site_url=site_url,
        description=strip_to_text(parsed.feed.get("subtitle") or parsed.feed.get("description") or ""),
        icon_url=_feed_icon(parsed, base),
        hub=_feed_hub(parsed, url),
        kind=parsed.get("version") or "",
    )
    feed_author = _author_name(parsed.feed)

    for entry in parsed.entries:
        link = _entry_link(entry, base)
        title = strip_to_text(entry.get("title") or "")
        published = (
            _struct_to_datetime(entry.get("published_parsed"))
            or _struct_to_datetime(entry.get("updated_parsed"))
            or _struct_to_datetime(entry.get("created_parsed"))
            or now
        )
        raw_html = _best_content(entry)
        content_html = sanitize_html(raw_html, base_url=link or base)
        guid = (entry.get("id") or "").strip() or link or _fallback_guid(title, published)
        enclosure_url, enclosure_type = _entry_enclosure(entry, base)
        author = _author_name(entry) or feed_author
        feed.entries.append(
            _finalise(
                ParsedEntry(
                    guid=guid,
                    url=link,
                    title=title,
                    author=strip_to_text(author) if author else None,
                    published_at=published,
                    content_html=content_html,
                    summary=_entry_summary(entry, raw_html),
                    enclosure_url=enclosure_url,
                    enclosure_type=enclosure_type,
                )
            )
        )
    return feed


# --------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------


def parse_feed(body: bytes | str, url: str | None = None, *, now: datetime | None = None) -> ParsedFeed:
    """Parse any supported feed format. Raises :class:`ParseError` when the body is not a feed."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    now = now or datetime.now(UTC)
    if _looks_like_json(body):
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ParseError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ParseError("JSON document is not an object")
        return parse_json_feed(data, url, now)
    return parse_xml_feed(body, url, now)


def looks_like_feed(body: bytes, content_type: str | None = None) -> bool:
    """Cheap check used by autodiscovery before a full parse."""
    ct = (content_type or "").lower()
    if any(t in ct for t in ("rss", "atom", "feed+json", "xml")):
        return True
    head = body.lstrip()[:512].lower()
    return head.startswith(b"{") or b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_TAGS",
    "IFRAME_HOSTS",
    "ParseError",
    "ParsedEntry",
    "ParsedFeed",
    "clean_srcset",
    "content_hash",
    "html_to_text",
    "iframe_src_allowed",
    "looks_like_feed",
    "normalise_for_hash",
    "parse_datetime",
    "parse_feed",
    "parse_json_feed",
    "parse_xml_feed",
    "sanitize_html",
    "strip_to_text",
]
