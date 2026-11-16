"""Exercise an existing-account upgrade, including old pause states, on a separate database."""

import asyncio
import os
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DB_URL


async def test_existing_account_subscription_migration():
    database = f"pensieve_migration_{uuid.uuid4().hex[:10]}"
    root = TEST_DB_URL.rsplit("/", 1)[0]
    admin = create_async_engine(root + "/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{database}"'))
    url = root + "/" + database
    engine = create_async_engine(url)

    async def migrate(revision):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            revision,
            env={**os.environ, "PENSIEVE_DATABASE_URL": url},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        assert process.returncode == 0, (out + err).decode()

    try:
        await migrate("b5c7d9e1f3a2")
        async with engine.begin() as conn:
            uid = uuid.uuid4()
            await conn.execute(
                text("""INSERT INTO users
                (id, email, display_name, password_hash, role, ai_enabled, settings)
                VALUES (:id, 'migration@example.org', 'Old account', 'unused', 'reader', false, '{}')"""),
                {"id": uid},
            )
            for reason, errors, error in [
                ("user", 0, None),
                ("errors", 10, "HTTP 500"),
                ("gone", 1, "Feed is gone (410)"),
            ]:
                await conn.execute(
                    text("""INSERT INTO feeds
                    (id, user_id, url, title, description, position, fetch_interval_min, error_count, last_error, paused)
                    VALUES (:id, :uid, :url, 'Old feed', '', 0, 15, :errors, :error, true)"""),
                    {
                        "id": uuid.uuid4(),
                        "uid": uid,
                        "url": f"https://example.org/{reason}",
                        "errors": errors,
                        "error": error,
                    },
                )
        await migrate("c8d9e0f1a2b3")
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT url, paused, unfollowed_at, pause_reason FROM feeds"))
            ).all()
            assert len(rows) == 3
            for url_, paused, unfollowed_at, reason in rows:
                assert url_.endswith(reason) and paused and unfollowed_at is None
            assert await conn.scalar(text("SELECT count(*) FROM subscription_events")) == 0
        await migrate("head")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM stream_follows")) == 0
            assert (
                await conn.scalar(text("SELECT count(*) FROM feeds WHERE essential OR delivery != 'normal'"))
                == 0
            )
            assert await conn.scalar(text("SELECT count(*) FROM users")) == 1
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()
