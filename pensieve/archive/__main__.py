"""Archive CLI.

  python -m pensieve.archive init-storage          bootstrap Garage (layout, key, bucket); idempotent
  python -m pensieve.archive check                 put/get/delete a probe object through the S3 API
  python -m pensieve.archive recapture <email>     re-queue every failed capture for a user
  python -m pensieve.archive archive-starred <email>  archive every starred item that has no snapshot yet
  python -m pensieve.archive gc                    delete blobs no snapshot references
"""

from __future__ import annotations

import asyncio
import logging
import sys


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    command = argv[0] if argv else ""
    if command == "init-storage":
        from pensieve.archive.garage import init_storage

        print(await init_storage())
        return 0
    if command == "check":
        from pensieve.archive.storage import get_storage

        storage = get_storage()
        if storage is None:
            print("archive storage is not configured (text-only mode)")
            return 1
        await storage.put("healthcheck/probe", b"ok", "text/plain")
        ok = await storage.get("healthcheck/probe") == b"ok"
        await storage.delete(["healthcheck/probe"])
        print("storage ok" if ok else "storage read-back mismatch")
        return 0 if ok else 1
    if command in {"recapture", "archive-starred"} and len(argv) > 1:
        from sqlalchemy import func, select

        from pensieve.archive import save
        from pensieve.db import session_scope
        from pensieve.models import User

        async with session_scope() as session:
            user = await session.scalar(select(User).where(func.lower(User.email) == argv[1].lower()))
            if user is None:
                print(f"no user {argv[1]}")
                return 1
            if command == "recapture":
                n = await save.requeue_failed(session, user.id)
            else:
                n = await save.archive_starred_backlog(session, user.id)
        print(f"queued {n} capture(s)")
        return 0
    if command == "gc":
        from pensieve.archive.jobs import collect_garbage

        print(f"removed {await collect_garbage()} blob(s)")
        return 0
    print(__doc__)
    return 2


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
