"""arq jobs for the capture worker (queue ``pensieve:capture``; ``pensieve.worker.CaptureWorkerSettings``)."""

from __future__ import annotations

import logging
import uuid

from arq import cron
from sqlalchemy import select

from pensieve.archive import save
from pensieve.archive.capture import capture_snapshot
from pensieve.archive.storage import asset_key, get_storage
from pensieve.db import session_scope
from pensieve.models import ArchiveAsset, Snapshot, SnapshotAsset

log = logging.getLogger(__name__)


async def capture_page(ctx: dict, snapshot_id: str, client_html: str | None = None) -> str:
    async with session_scope() as session:
        try:
            snap = await capture_snapshot(session, uuid.UUID(snapshot_id), client_html=client_html)
        except Exception as exc:
            log.exception("capture %s crashed", snapshot_id)
            await session.rollback()
            snap = await session.get(Snapshot, uuid.UUID(snapshot_id))
            if snap is not None:
                snap.status = "failed" if snap.attempts >= 3 else "queued"
                snap.error = f"capture crashed: {exc.__class__.__name__}: {exc}"[:2000]
                await session.commit()
            raise
        return snap.status if snap else "gone"


CAPTURE_QUEUE_TARGET = 30  # the sweep tops the capture queue up to this many waiting jobs


async def capture_sweep(ctx: dict) -> int:
    """Cron: revive captures a dead worker left behind, and trickle waiting ones (imports) into the queue."""
    from pensieve import queue

    try:
        pool = await queue.get_pool()
        depth = int(await pool.zcard(queue.CAPTURE_QUEUE))
    except Exception:  # noqa: BLE001
        depth = 0
    async with session_scope() as session:
        retry, failed = await save.stale_captures(session, room=max(0, CAPTURE_QUEUE_TARGET - depth))
    for sid in retry:
        await save.enqueue_capture(sid)
    if retry or failed:
        log.info("capture sweep: queued %d, gave up on %d (queue depth was %d)", len(retry), failed, depth)
    return len(retry)


async def import_links(ctx: dict, user_id: str, links: list[dict]) -> int:
    """Background import of a Pocket / Instapaper / bookmarks export (links parsed by the web request)."""
    from datetime import UTC, datetime

    from pensieve.models import User

    count = 0
    async with session_scope() as session:
        user = await session.get(User, uuid.UUID(user_id))
        if user is None:
            return 0
        for n, link in enumerate(links, 1):
            try:
                saved_at = datetime.fromtimestamp(link["saved_at"], UTC) if link.get("saved_at") else None
                async with session.begin_nested():
                    await save.save_link(
                        session,
                        user,
                        link["url"],
                        title=link.get("title"),
                        tags=link.get("tags") or [],
                        read=bool(link.get("read")),
                        starred=bool(link.get("starred")),
                        saved_at=saved_at,
                        capture=False,
                        check=False,
                        commit=False,
                    )
                count += 1
            except save.SaveError:
                continue
            except Exception as exc:  # noqa: BLE001 - one bad row must not sink the import
                log.info("import: skipped %s: %s", link.get("url"), exc)
            if n % 200 == 0:
                await session.commit()
        await session.commit()
    save._invalidate_nav(uuid.UUID(user_id))
    log.info("import for %s: %d links saved; captures trickle in via the sweep", user_id, count)
    return count


async def collect_garbage(ctx: dict | None = None) -> int:
    """Delete blobs nothing references: assets no snapshot uses, and capture folders of deleted snapshots."""
    storage = get_storage()
    if storage is None:
        return 0
    removed = 0
    async with session_scope() as session:
        orphans = (
            await session.scalars(
                select(ArchiveAsset.sha256).where(
                    ~select(SnapshotAsset.sha256).where(SnapshotAsset.sha256 == ArchiveAsset.sha256).exists()
                )
            )
        ).all()
        if orphans:
            await storage.delete([asset_key(sha) for sha in orphans])
            await session.execute(ArchiveAsset.__table__.delete().where(ArchiveAsset.sha256.in_(orphans)))
            await session.commit()
            removed += len(orphans)
        live = {str(row) for row in (await session.scalars(select(Snapshot.id))).all()}
        generations = {
            str(sid): gen for sid, gen in (await session.execute(select(Snapshot.id, Snapshot.generation))).all()
        }
    stale: list[str] = []
    async for key in storage.list_prefix("snap/"):
        parts = key.split("/")
        if len(parts) < 4:
            continue
        sid, gen = parts[1], parts[2]
        if sid not in live or (gen.isdigit() and int(gen) != generations.get(sid)):
            stale.append(key)
    if stale:
        await storage.delete(stale)
        removed += len(stale)
    return removed


async def gc_job(ctx: dict) -> int:
    return await collect_garbage(ctx)


FUNCTIONS = [capture_page, capture_sweep, gc_job, import_links]
CRON_JOBS = [
    cron(capture_sweep, minute=set(range(1, 60, 5)), unique=True),
    cron(gc_job, weekday=6, hour=4, minute=40, unique=True),
]

__all__ = ["CRON_JOBS", "FUNCTIONS", "capture_page", "capture_sweep", "collect_garbage", "import_links"]
