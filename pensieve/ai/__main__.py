"""Manual runs: ``python -m pensieve.ai digest <email> | profile <email> | tag <item_uuid> | health``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select

from pensieve import models
from pensieve.ai import categorize, insights, memory
from pensieve.ai.client import LLMClient
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
    day = date.fromisoformat(args.day) if args.day else datetime.now(UTC).date()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pensieve.ai")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("digest", help="generate today's digest for a user")
    p.add_argument("email")
    p.add_argument("--day", help="YYYY-MM-DD (default today)")
    p.set_defaults(fn=cmd_digest)
    p = sub.add_parser("profile", help="refresh the reader profile for a user")
    p.add_argument("email")
    p.set_defaults(fn=cmd_profile)
    p = sub.add_parser("tag", help="tag one item")
    p.add_argument("item_uuid")
    p.set_defaults(fn=cmd_tag)
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
