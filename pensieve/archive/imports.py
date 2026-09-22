"""Parse read-later exports: Pocket (HTML and the newer CSV), Instapaper CSV, and browser bookmarks HTML."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from lxml import html as lxml_html

MAX_LINKS = 20_000


@dataclass
class ImportedLink:
    url: str
    title: str = ""
    tags: list[str] = field(default_factory=list)
    saved_at: float | None = None
    read: bool = False
    starred: bool = False

    def as_job(self) -> dict:
        return asdict(self)


def _epoch(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        n = int(value)
        return n / 1000 if n > 10_000_000_000 else float(n)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)  # noqa: DTZ007 - naive export times are taken as UTC below
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
        except ValueError:
            continue
    return None


def _tags(value: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[|,;]", value or "") if t.strip()][:20]


def _from_html(data: str) -> list[ImportedLink]:
    """Pocket's ril_export.html and Netscape bookmark files: <a href> with time_added/add_date and tags."""
    tree = lxml_html.fromstring(data or "<html></html>")
    links: list[ImportedLink] = []
    section_read = False
    for el in tree.iter("h1", "h2", "h3", "a"):
        if el.tag in {"h1", "h2", "h3"}:
            heading = (el.text_content() or "").lower()
            section_read = "archive" in heading or "read" in heading and "unread" not in heading
            continue
        href = (el.get("href") or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        links.append(
            ImportedLink(
                url=href,
                title=" ".join((el.text_content() or "").split()),
                tags=_tags(el.get("tags")),
                saved_at=_epoch(el.get("time_added") or el.get("add_date")),
                read=section_read,
            )
        )
    return links


def _from_csv(data: str) -> list[ImportedLink]:
    reader = csv.DictReader(io.StringIO(data))
    fields = {f.lower().strip(): f for f in (reader.fieldnames or [])}
    url_key = fields.get("url") or next((v for k, v in fields.items() if "url" in k), None)
    if url_key is None:
        return []
    title_key = fields.get("title")
    time_key = fields.get("time_added") or fields.get("timestamp") or fields.get("created") or fields.get("saved_at")
    tags_key = fields.get("tags")
    status_key = fields.get("status") or fields.get("folder")
    links: list[ImportedLink] = []
    for row in reader:
        url = (row.get(url_key) or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        status = (row.get(status_key) or "").strip().lower() if status_key else ""
        links.append(
            ImportedLink(
                url=url,
                title=" ".join((row.get(title_key) or "").split()) if title_key else "",
                tags=_tags(row.get(tags_key)) if tags_key else [],
                saved_at=_epoch(row.get(time_key)) if time_key else None,
                read=status in {"archive", "archived", "read"},
                starred=status in {"starred", "favorite", "favourite"},
            )
        )
    return links


def parse_export(filename: str, data: bytes) -> list[ImportedLink]:
    """Links from an export file, deduplicated by URL (last one wins), capped at ``MAX_LINKS``."""
    text = data.decode("utf-8-sig", errors="replace")
    head = text.lstrip()[:400].lower()
    if filename.lower().endswith((".html", ".htm")) or head.startswith(("<!doctype", "<html", "<meta", "<title")):
        links = _from_html(text)
    else:
        links = _from_csv(text)
    unique: dict[str, ImportedLink] = {}
    for link in links:
        unique[link.url] = link
    return list(unique.values())[:MAX_LINKS]


__all__ = ["ImportedLink", "parse_export"]
