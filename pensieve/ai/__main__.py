"""Manual runs: ``python -m pensieve.ai digest | paper | profile | tag | backfill | summaries | health``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from pensieve import models
from pensieve.ai import categorize, insights, memory, paper
from pensieve.ai.client import LLMClient
from pensieve.config import get_settings
from pensieve.db import dispose_engine, session_scope


async def _user_by_email(session, email: str) -> models.User:
    user = await session.scalar(select(models.User).where(models.User.email == email))
    if user is None:
        sys.exit(f"no user with email {email}")
    return user


async def cmd_health(_: argparse.Namespace) -> int:
    client = LLMClient()
    try:
        print(json.dumps(await client.health(), indent=1))
    finally:
        await client.aclose()
    return 0


async def cmd_digest(args: argparse.Namespace) -> int:
    day = date.fromisoformat(args.day) if args.day else datetime.now(ZoneInfo(get_settings().timezone)).date()
    async with session_scope() as session:
        user = await _user_by_email(session, args.email)
        row = await insights.daily_digest(session, user, day)
        if row is None:
            print("digest disabled for this user")
            return 1
        print(
            json.dumps(
                {"id": str(row.id), "period": row.period, "title": row.title, "body": row.body}, indent=1
            )
        )
    return 0


async def cmd_paper(args: argparse.Namespace) -> int:
    day = date.fromisoformat(args.day) if args.day else paper.today()
    async with session_scope() as session:
        user = await _user_by_email(session, args.email)
        row = await paper.daily_paper(session, user, day)
        body = row.body
        print(f"{row.title}: {body['story_count']} stories from {body['item_count']} items")
        for sec in body["sections"]:
            print(f"  {sec['title']}: {sec['count']} ({sec['unread']} unread)")
    return 0


async def cmd_summaries(args: argparse.Namespace) -> int:
    """Enqueue eager summaries for the user's recent items that have none yet (one job per feed chunk)."""
    from datetime import timedelta

    from pensieve import queue
    from pensieve.ai.common import utcnow

    cap = get_settings().ai_max_items_per_job
    since = utcnow() - timedelta(days=args.days)
    async with session_scope() as session:
        user = await _user_by_email(session, args.email)
        feeds = list((await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all())
        total = 0
        for feed in feeds:
            stmt = (
                select(models.Item.id)
                .where(
                    models.Item.feed_id == feed.id,
                    models.Item.published_at >= since,
                    ~select(models.ItemAI.item_id)
                    .where(
                        models.ItemAI.item_id == models.Item.id,
                        models.ItemAI.user_id == user.id,
                        models.ItemAI.summary.is_not(None),
                    )
                    .exists(),
                )
                .order_by(models.Item.published_at.desc())
            )
            ids = [str(i) for i in (await session.scalars(stmt)).all()]
            for n, start in enumerate(range(0, len(ids), cap)):
                chunk = ids[start : start + cap]
                await queue.enqueue(
                    queue.AI_SUMMARIZE_ITEMS,
                    str(user.id),
                    chunk,
                    _job_id=f"{queue.job_id_for('summaries', feed.id)}:{n}",
                )
                total += len(chunk)
            if ids:
                print(f"{feed.title}: {len(ids)} without a summary -> {(len(ids) + cap - 1) // cap} job(s)")
        print(f"enqueued {total} items in chunks of {cap}; duplicates within a story are skipped by the job")
    return 0


async def cmd_profile(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        user = await _user_by_email(session, args.email)
        row = await memory.refresh_profile(session, user)
        if row is None:
            print("memory disabled for this user")
            return 1
        print(f"profile v{row.version}\n\n{row.body_text}\n")
        if row.diff_from_previous:
            print(row.diff_from_previous)
    return 0


async def cmd_tag(args: argparse.Namespace) -> int:
    item_id = uuid.UUID(args.item_uuid)
    async with session_scope() as session:
        item = await session.get(models.Item, item_id)
        if item is None:
            sys.exit(f"no item {item_id}")
        feed = await session.get(models.Feed, item.feed_id)
        user = await session.get(models.User, feed.user_id)
        rows = await categorize.tag_items(session, user, [item])
        for r in rows:
            print(
                json.dumps(
                    {"tags": r.tags, "confidences": r.confidences, "content_type": r.content_type}, indent=1
                )
            )
        if not rows:
            print("tagging disabled or nothing written")
    return 0


async def cmd_backfill(args: argparse.Namespace) -> int:
    """Enqueue process jobs for every item of the user that has no item_ai row yet, newest first, in capped chunks."""
    from pensieve import queue

    cap = get_settings().ai_max_items_per_job
    async with session_scope() as session:
        user = await _user_by_email(session, args.email)
        feeds = list((await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all())
        total = 0
        for feed in feeds:
            untagged = (
                select(models.Item.id)
                .where(
                    models.Item.feed_id == feed.id,
                    ~select(models.ItemAI.item_id)
                    .where(models.ItemAI.item_id == models.Item.id, models.ItemAI.user_id == user.id)
                    .exists(),
                )
                .order_by(models.Item.published_at.desc())
            )
            if args.limit:
                untagged = untagged.limit(args.limit)
            ids = [str(i) for i in (await session.scalars(untagged)).all()]
            for n, start in enumerate(range(0, len(ids), cap)):
                chunk = ids[start : start + cap]
                await queue.enqueue(
                    queue.AI_PROCESS_NEW_ITEMS,
                    str(feed.id),
                    chunk,
                    _job_id=f"{queue.job_id_for('backfill', feed.id)}:{n}",
                )
                total += len(chunk)
            print(f"{feed.title}: {len(ids)} untagged -> {(len(ids) + cap - 1) // cap} job(s)")
        print(f"enqueued {total} items in chunks of {cap}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pensieve.ai")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("digest", help="generate today's digest for a user")
    p.add_argument("email")
    p.add_argument("--day", help="YYYY-MM-DD (default today)")
    p.set_defaults(fn=cmd_digest)
    p = sub.add_parser("paper", help="compile today's paper for a user (no model call)")
    p.add_argument("email")
    p.add_argument("--day", help="YYYY-MM-DD (default today)")
    p.set_defaults(fn=cmd_paper)
    p = sub.add_parser("summaries", help="enqueue eager summaries for recent items that have none")
    p.add_argument("email")
    p.add_argument("--days", type=int, default=2, help="look back this many days (default 2)")
    p.set_defaults(fn=cmd_summaries)
    p = sub.add_parser("profile", help="refresh the reader profile for a user")
    p.add_argument("email")
    p.set_defaults(fn=cmd_profile)
    p = sub.add_parser("tag", help="tag one item")
    p.add_argument("item_uuid")
    p.set_defaults(fn=cmd_tag)
    p = sub.add_parser("backfill", help="enqueue AI processing for items that were never tagged")
    p.add_argument("email")
    p.add_argument("--limit", type=int, default=0, help="newest N per feed (default: all untagged)")
    p.set_defaults(fn=cmd_backfill)
    p = sub.add_parser("health", help="gateway health")
    p.set_defaults(fn=cmd_health)
    args = parser.parse_args(argv)

    async def run() -> int:
        try:
            return await args.fn(args)
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
