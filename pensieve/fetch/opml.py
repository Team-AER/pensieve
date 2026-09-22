"""OPML import and export. Import never fetches: new feeds are due immediately for the scheduler."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from lxml import etree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.config import get_settings
from pensieve.models import Feed, Folder, User

log = logging.getLogger(__name__)


class OPMLError(ValueError):
    """The document is not OPML."""


@dataclass(slots=True)
class ImportResult:
    added: list[Feed] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    folders_created: list[Folder] = field(default_factory=list)


@dataclass(slots=True)
class _Outline:
    xml_url: str
    html_url: str | None
    title: str
    folder: str | None


def _attr(el: etree._Element, *names: str) -> str | None:
    for name in names:
        value = el.get(name)
        if value is None:
            # OPML attributes are case-insensitive in the wild (xmlUrl / xmlurl / XMLURL).
            for key, val in el.attrib.items():
                if key.lower() == name.lower():
                    value = val
                    break
        if value is not None and value.strip():
            return value.strip()
    return None


def parse_opml(xml: bytes | str) -> list[_Outline]:
    """Flatten OPML outlines to (feed, folder) pairs. Nested folders collapse to their top-level ancestor."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    try:
        root = etree.fromstring(xml, parser)
    except etree.XMLSyntaxError as exc:
        raise OPMLError(f"invalid XML: {exc}") from exc
    if root is None or etree.QName(root).localname.lower() != "opml":
        raise OPMLError("not an OPML document")
    body = root.find("body")
    if body is None:
        raise OPMLError("OPML has no <body>")

    outlines: list[_Outline] = []

    def walk(el: etree._Element, folder: str | None) -> None:
        for child in el:
            if not isinstance(child.tag, str) or etree.QName(child).localname.lower() != "outline":
                continue
            xml_url = _attr(child, "xmlUrl")
            if xml_url:
                title = _attr(child, "title", "text") or xml_url
                outlines.append(_Outline(xml_url, _attr(child, "htmlUrl"), title, folder))
                # Feeds occasionally nest other outlines; keep them in the same folder.
                walk(child, folder)
            else:
                name = _attr(child, "text", "title")
                walk(child, folder if folder is not None else name)

    walk(body, None)
    return outlines


async def import_opml(session: AsyncSession, user: User, xml: bytes) -> ImportResult:
    """Create folders and feeds for ``user`` from OPML. Duplicates (existing or repeated) are skipped."""
    outlines = parse_opml(xml)
    result = ImportResult()
    now = datetime.now(UTC)
    settings = get_settings()

    existing_urls = set((await session.scalars(select(Feed.url).where(Feed.user_id == user.id))).all())
    folders = {
        f.name: f for f in (await session.scalars(select(Folder).where(Folder.user_id == user.id))).all()
    }
    next_position = max((f.position for f in folders.values()), default=-1) + 1

    for outline in outlines:
        url = outline.xml_url
        if url in existing_urls:
            result.skipped.append(url)
            continue
        folder: Folder | None = None
        if outline.folder:
            name = outline.folder[:120]
            folder = folders.get(name)
            if folder is None:
                folder = Folder(id=uuid.uuid4(), user_id=user.id, name=name, position=next_position)
                next_position += 1
                session.add(folder)
                folders[name] = folder
                result.folders_created.append(folder)
        feed = Feed(
            id=uuid.uuid4(),
            user_id=user.id,
            folder_id=folder.id if folder else None,
            url=url[:2048],
            site_url=outline.html_url[:2048] if outline.html_url else None,
            title=outline.title[:300],
            fetch_interval_min=settings.fetch_default_interval_min,
            next_fetch_at=now,
        )
        session.add(feed)
        existing_urls.add(url)
        result.added.append(feed)

    await session.flush()
    await session.commit()  # rows must be visible before the AI worker looks them up
    # Feeds that landed without a folder get an AI filing suggestion, exactly as add_feed does.
    for feed in result.added:
        if feed.folder_id is None:
            try:
                await queue.enqueue(
                    queue.AI_FILE_FEED, str(feed.id), _job_id=queue.job_id_for(queue.AI_FILE_FEED, feed.id)
                )
            except Exception as exc:  # noqa: BLE001 - a missing Redis must never fail an import
                log.warning("could not enqueue %s for feed %s: %s", queue.AI_FILE_FEED, feed.id, exc)
    return result


def _feed_outline(parent: ET.Element, feed: Feed) -> None:
    attrs = {
        "type": "rss",
        "text": feed.title or feed.url,
        "title": feed.title or feed.url,
        "xmlUrl": feed.url,
    }
    if feed.site_url:
        attrs["htmlUrl"] = feed.site_url
    ET.SubElement(parent, "outline", attrs)


def export_opml(user: User, feeds: list[Feed], folders: list[Folder]) -> bytes:
    """OPML 2.0 with one outline per folder (feeds inside) and unfiled feeds at the top level."""
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = f"Pensieve subscriptions ({user.email})"
    ET.SubElement(head, "dateCreated").text = format_datetime(datetime.now(UTC))
    ET.SubElement(head, "ownerEmail").text = user.email
    body = ET.SubElement(root, "body")

    by_folder: dict[uuid.UUID | None, list[Feed]] = {}
    for feed in sorted(feeds, key=lambda f: (f.position, (f.title or "").lower())):
        by_folder.setdefault(feed.folder_id, []).append(feed)

    for folder in sorted(folders, key=lambda f: (f.position, f.name.lower())):
        members = by_folder.pop(folder.id, [])
        node = ET.SubElement(body, "outline", {"text": folder.name, "title": folder.name})
        for feed in members:
            _feed_outline(node, feed)
    for feed in by_folder.pop(None, []):
        _feed_outline(body, feed)
    # Feeds whose folder was not passed still get exported, unfiled.
    for members in by_folder.values():
        for feed in members:
            _feed_outline(body, feed)

    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


__all__ = ["ImportResult", "OPMLError", "export_opml", "import_opml", "parse_opml"]
