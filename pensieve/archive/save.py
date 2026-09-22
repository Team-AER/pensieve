"""Save a link (Pocket-style) and schedule its capture; archive starred feed items the same way.

Saved links are items of a per-user system feed (``Feed.kind == 'saved'``), so read state, stars, tags, notes,
search, the sync APIs, retention and the whole AI pipeline treat them like any other item. In reader terms
unread is "My list" and read is "Archive".
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import queue
from pensieve.fetch.http import UnsafeURLError, ensure_safe_url
from pensieve.fetch.parse import content_hash
from pensieve.models import (
    FEED_KIND_RSS,
    FEED_KIND_SAVED,
    SAVED_FEED_URL,
    Feed,
    Item,
    ItemState,
    Note,
    Snapshot,
    User,
)

log = logging.getLogger(__name__)

TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|gbraid|wbraid|msclkid|mc_cid|mc_eid|igshid|yclid|_hsenc|_hsmi|mkt_tok|"
    r"ref_src|ref_url|spm|si|vero_id|oly_enc_id|oly_anon_id|__s|s_cid|cmpid|ncid)$",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
CLIENT_HTML_MAX = 8 * 1024 * 1024
_HOST = re.compile(r"^[a-z0-9._:\[\]-]+$", re.IGNORECASE)
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)")  # "example.com:8080/x" is a host and port, not a scheme


class SaveError(ValueError):
    """The link can't be saved (malformed, or points somewhere Pensieve must not fetch)."""


@dataclass
class SaveResult:
    item: Item
    snapshot: Snapshot
    created: bool


def find_url(*candidates: str | None) -> str | None:
    """The first http(s) URL in the given values: share sheets put it in ``url``, ``text`` or ``title``."""
    for value in candidates:
        value = (value or "").strip()
        if not value:
            continue
        if value.lower().startswith(("http://", "https://")) and " " not in value:
            return value
        match = _URL_IN_TEXT.search(value)
        if match:
            return match.group(0).rstrip(".,;:!?)]}")
    for value in candidates:
        value = (value or "").strip()
        if value and " " not in value and "." in value and "/" not in value.split(".")[0]:
            return value  # a bare domain like example.com/post
    return None


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise SaveError("Enter a link to save.")
    scheme = _SCHEME.match(raw)
    if scheme and scheme.group(1).lower() not in {"http", "https"}:
        raise SaveError("Only http and https links can be saved.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise SaveError("That doesn't look like a link.") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise SaveError("Only http and https links can be saved.")
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SaveError("That doesn't look like a link.") from exc
    if not _HOST.match(host) or ("." not in host and ":" not in host):
        raise SaveError("That doesn't look like a link.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    netloc = parts.netloc.lower() if not (parts.username or parts.password) else parts.netloc
    path = parts.path or "/"
    url = urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query, doseq=True), ""))
    if len(url) > 2048:
        raise SaveError("That link is too long to save.")
    return url


async def saved_feed(session: AsyncSession, user_id: uuid.UUID) -> Feed:
    """The user's saved-links feed, created on first use (race-safe via the partial unique index)."""
    stmt = select(Feed).where(Feed.user_id == user_id, Feed.kind == FEED_KIND_SAVED)
    feed = await session.scalar(stmt)
    if feed is not None:
        return feed
    try:
        async with session.begin_nested():
            feed = Feed(
                user_id=user_id,
                url=SAVED_FEED_URL,
                title="Saved",
                description="Links you saved",
                kind=FEED_KIND_SAVED,
                paused=True,
                next_fetch_at=None,
                fetch_interval_min=24 * 60,
            )
            session.add(feed)
            await session.flush()
    except IntegrityError:
        feed = await session.scalar(stmt)
    return feed


async def ensure_snapshot(
    session: AsyncSession, user_id: uuid.UUID, item: Item, url: str, *, force: bool = False
) -> tuple[Snapshot, bool]:
    """(snapshot, needs_capture). A done capture is kept unless ``force``; a failed one is retried."""
    snap = await session.scalar(select(Snapshot).where(Snapshot.user_id == user_id, Snapshot.item_id == item.id))
    if snap is None:
        snap = Snapshot(user_id=user_id, item_id=item.id, requested_url=url[:2048], status="queued", attempts=0,
                        generation=0, word_count=0, bytes_total=0)
        session.add(snap)
        await session.flush()
        return snap, True
    if force or snap.status == "failed":
        snap.status = "queued"
        snap.attempts = 0
        snap.error = None
        snap.requested_url = url[:2048]
        return snap, True
    return snap, snap.status == "queued"


async def enqueue_capture(snapshot_id: uuid.UUID, client_html: str | None = None) -> bool:
    kwargs = {"client_html": client_html[:CLIENT_HTML_MAX]} if client_html else {}
    try:
        await queue.enqueue(
            queue.CAPTURE_PAGE, str(snapshot_id), _job_id=queue.job_id_for(queue.CAPTURE_PAGE, snapshot_id), **kwargs
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the sweep cron re-queues anything left in 'queued'
        log.warning("could not enqueue capture for %s: %s", snapshot_id, exc)
        return False


async def save_link(
    session: AsyncSession,
    user: User,
    url: str,
    *,
    title: str | None = None,
    tags: list[str] | tuple[str, ...] = (),
    note: str | None = None,
    client_html: str | None = None,
    read: bool = False,
    starred: bool = False,
    saved_at: datetime | None = None,
    capture: bool = True,
    check: bool = True,
    commit: bool = True,
) -> SaveResult:
    """Save ``url`` for ``user`` (idempotent: a known link moves back to the top of My list), commit, queue it.

    Imports pass ``check=False`` (no DNS per link; the capture's own fetch applies the SSRF guard),
    ``capture=False`` (the sweep trickles captures in) and ``commit=False`` (one commit per batch).
    """
    url = normalize_url(url)
    if check:
        try:
            await ensure_safe_url(url)
        except UnsafeURLError as exc:
            raise SaveError("Pensieve can't fetch that address (it points at a private or local network).") from exc
    feed = await saved_feed(session, user.id)
    now = datetime.now(UTC)
    title = " ".join((title or "").split())[:1000] or None
    item = await session.scalar(select(Item).where(Item.feed_id == feed.id, Item.guid == url))
    created = item is None
    if item is None:
        item = Item(
            feed_id=feed.id,
            guid=url,
            url=url,
            title=title or url,
            published_at=saved_at or now,
            content_html="",
            content_text="",
            hash=content_hash(title or url, url),
        )
        session.add(item)
        await session.flush()
    else:
        if saved_at is None:
            item.published_at = now  # re-saving moves it back to the top of My list
        if title and item.title in {"", item.url}:
            item.title = title
    clean_tags = sorted({t.strip().lower()[:120] for t in tags if t and t.strip()})
    existing = await session.get(ItemState, (user.id, item.id))
    merged = sorted(set(existing.tags if existing and existing.tags else []) | set(clean_tags))
    values = {"is_read": read, "read_at": now if read else None, "tags": merged}
    if starred:
        values |= {"is_starred": True, "starred_at": now}
    await session.execute(
        pg_insert(ItemState)
        .values({"user_id": user.id, "item_id": item.id, "is_starred": False, "hidden": False} | values)
        .on_conflict_do_update(index_elements=["user_id", "item_id"], set_=values)
    )
    if existing is not None:
        await session.refresh(existing)
    if note and note.strip():
        session.add(Note(user_id=user.id, item_id=item.id, quote="", body=note.strip()[:10_000]))
    snapshot, needs = await ensure_snapshot(session, user.id, item, url, force=bool(client_html))
    if not commit:
        await session.flush()
        return SaveResult(item=item, snapshot=snapshot, created=created)
    await session.commit()
    _invalidate_nav(user.id)
    if needs and capture:
        await enqueue_capture(snapshot.id, client_html)
    return SaveResult(item=item, snapshot=snapshot, created=created)


def _invalidate_nav(user_id: uuid.UUID) -> None:
    try:
        from pensieve.web.queries import invalidate_nav_cache

        invalidate_nav_cache(user_id)
    except Exception as exc:  # noqa: BLE001 - the counts expire on their own within seconds
        log.debug("nav cache not invalidated: %s", exc)


def wants_starred_archive(user: User) -> bool:
    return bool((user.settings or {}).get("archive_starred", True))


async def archive_items(session: AsyncSession, user: User, item_ids: list[uuid.UUID]) -> int:
    """Queue captures for starred feed items (idempotent). Commits. Returns how many were queued."""
    if not item_ids or not wants_starred_archive(user):
        return 0
    rows = (
        await session.execute(
            select(Item)
            .join(Feed, Feed.id == Item.feed_id)
            .where(Item.id.in_(item_ids), Feed.user_id == user.id, Feed.kind == FEED_KIND_RSS, Item.url.is_not(None))
        )
    ).scalars().all()
    todo: list[uuid.UUID] = []
    for item in rows:
        try:
            url = normalize_url(item.url or "")
        except SaveError:
            continue
        snap, needs = await ensure_snapshot(session, user.id, item, url)
        if needs:
            todo.append(snap.id)
    await session.commit()
    for sid in todo:
        await enqueue_capture(sid)
    return len(todo)


async def archive_after_star(session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]) -> None:
    """Best-effort hook for every "star" path (web, Google Reader API, Fever). Never raises."""
    try:
        user = await session.get(User, user_id)
        if user is not None:
            await archive_items(session, user, item_ids)
    except Exception as exc:  # noqa: BLE001 - archiving is additive; starring must always succeed
        log.warning("could not queue archive for starred items %s: %s", item_ids[:3], exc)


async def archive_starred_backlog(session: AsyncSession, user_id: uuid.UUID, limit: int = 5000) -> int:
    user = await session.get(User, user_id)
    if user is None:
        return 0
    have = select(Snapshot.item_id).where(Snapshot.user_id == user_id)
    ids = (
        await session.scalars(
            select(ItemState.item_id)
            .where(ItemState.user_id == user_id, ItemState.is_starred.is_(True), ItemState.item_id.not_in(have))
            .limit(limit)
        )
    ).all()
    settings = dict(user.settings or {})
    if settings.get("archive_starred") is False:
        return 0
    return await archive_items(session, user, list(ids))


async def requeue_failed(session: AsyncSession, user_id: uuid.UUID) -> int:
    snaps = (
        await session.scalars(select(Snapshot).where(Snapshot.user_id == user_id, Snapshot.status == "failed"))
    ).all()
    for s in snaps:
        s.status, s.attempts, s.error = "queued", 0, None
    await session.commit()
    for s in snaps:
        await enqueue_capture(s.id)
    return len(snaps)


async def stale_captures(
    session: AsyncSession, room: int, now: datetime | None = None
) -> tuple[list[uuid.UUID], int]:
    """Captures to (re-)queue now: ones a dead worker left in 'rendering', then the oldest waiting ones.

    ``room`` is how many jobs the capture queue can take this round, so a 3,000-link import trickles in
    instead of burying a link saved a minute ago. Waiting rows are touched when handed out, so the next
    round moves on to the following ones. After 3 attempts a capture is marked failed.
    """
    now = now or datetime.now(UTC)
    failed = 0
    dead = (
        await session.scalars(
            select(Snapshot).where(Snapshot.status == "rendering", Snapshot.updated_at < now - timedelta(minutes=15))
        )
    ).all()
    for s in dead:
        if s.attempts >= 3:
            s.status, s.error = "failed", (s.error or "capture did not finish after 3 attempts")
            failed += 1
        else:
            s.status = "queued"
            s.updated_at = now - timedelta(minutes=11)
    await session.flush()
    retry: list[uuid.UUID] = []
    if room > 0:
        waiting = (
            await session.scalars(
                select(Snapshot)
                .where(Snapshot.status == "queued", Snapshot.updated_at < now - timedelta(minutes=10))
                .order_by(Snapshot.created_at)
                .limit(room)
            )
        ).all()
        for s in waiting:
            s.updated_at = now
            retry.append(s.id)
    await session.commit()
    return retry, failed


__all__ = [
    "SaveError",
    "SaveResult",
    "archive_after_star",
    "archive_items",
    "archive_starred_backlog",
    "enqueue_capture",
    "ensure_snapshot",
    "find_url",
    "normalize_url",
    "requeue_failed",
    "save_link",
    "saved_feed",
    "stale_captures",
]
