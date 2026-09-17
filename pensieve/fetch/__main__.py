"""Manual testing helpers.

    python -m pensieve.fetch refresh <feed_url_or_id>
    python -m pensieve.fetch import <user_email> <file.opml>
    python -m pensieve.fetch discover <url>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from pensieve.db import dispose_engine, session_scope
from pensieve.models import Feed, User


async def _refresh(target: str) -> int:
    from pensieve.fetch.subscribe import refresh_feed

    async with session_scope() as session:
        feed: Feed | None = None
        try:
            feed = await session.get(Feed, uuid.UUID(target))
        except ValueError:
            feed = await session.scalar(select(Feed).where(Feed.url == target).limit(1))
        if feed is None:
            print(f"no feed matching {target!r}", file=sys.stderr)
            return 1
        items = await refresh_feed(session, feed)
        print(f"{feed.title or feed.url}: {len(items)} new item(s); next fetch in {feed.fetch_interval_min} min")
        if feed.last_error:
            print(f"last error: {feed.last_error}", file=sys.stderr)
        for item in items:
            print(f"  - {item.published_at:%Y-%m-%d} {item.title[:80]}")
    return 0


async def _import(email: str, path: Path) -> int:
    from pensieve.fetch.opml import import_opml

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"no user with email {email!r}", file=sys.stderr)
            return 1
        result = await import_opml(session, user, path.read_bytes())
        print(
            f"added {len(result.added)} feed(s), skipped {len(result.skipped)}, "
            f"created {len(result.folders_created)} folder(s)"
        )
    return 0


async def _discover(url: str) -> int:
    from pensieve.fetch.discover import DiscoveryError, discover

    try:
        found = await discover(url)
    except DiscoveryError as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 1
    print(f"feed: {found.feed_url}\ntitle: {found.parsed.title}\nsite: {found.site_url}")
    print(f"icon: {found.icon_url}\nhub: {found.hub}\nentries: {len(found.parsed.entries)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pensieve.fetch")
    sub = parser.add_subparsers(dest="command", required=True)
    p_refresh = sub.add_parser("refresh", help="poll one feed by URL or id")
    p_refresh.add_argument("target")
    p_import = sub.add_parser("import", help="import an OPML file for a user")
    p_import.add_argument("email")
    p_import.add_argument("file", type=Path)
    p_discover = sub.add_parser("discover", help="autodiscover a feed for a URL without storing it")
    p_discover.add_argument("url")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def run() -> int:
        try:
            if args.command == "refresh":
                return await _refresh(args.target)
            if args.command == "import":
                return await _import(args.email, args.file)
            return await _discover(args.url)
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
