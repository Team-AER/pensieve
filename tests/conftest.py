"""Shared test fixtures.

Tests run against the docker-compose Postgres (pgvector) on localhost. Each package's tests may set
PENSIEVE_TEST_DATABASE_URL to its own database name to run in parallel with other packages' tests.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB_URL = os.environ.get(
    "PENSIEVE_TEST_DATABASE_URL", "postgresql+asyncpg://pensieve:pensieve@localhost:5432/pensieve_test"
)
os.environ["PENSIEVE_DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("PENSIEVE_SECRET_KEY", "test-secret-key-not-for-prod")
os.environ["PENSIEVE_DEBUG"] = "false"  # a developer .env with debug=true must not relax the SSRF guard in tests
os.environ.setdefault("PENSIEVE_REDIS_URL", "redis://localhost:6379/9")

from pensieve import models
from pensieve.auth import hash_password
from pensieve.db import Base, dispose_engine


async def _ensure_database() -> None:
    """Create the test database if missing (connect to the maintenance db first)."""
    admin_url = TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"
    db_name = TEST_DB_URL.rsplit("/", 1)[1]
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name})
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await engine.dispose()


@pytest.fixture(scope="session")
async def engine():
    await _ensure_database()
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    await dispose_engine()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A session whose work is committed (tests hit the real DB); tables are truncated after each test."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    async with engine.begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def user(session: AsyncSession) -> models.User:
    u = models.User(
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
        password_hash=hash_password("password123"),
        role=models.UserRole.admin,
    )
    session.add(u)
    await session.commit()
    return u


@pytest.fixture
async def client(engine) -> AsyncIterator[AsyncClient]:
    """ASGI client against the real app, sharing the test database."""
    from pensieve.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
