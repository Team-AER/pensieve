# ruff: noqa: F811
"""Opt-in: a real capture through the compose `browser` and Garage (PENSIEVE_IT=1, `make dev-db` running).

  PENSIEVE_IT=1 PENSIEVE_IT_S3_ACCESS_KEY=... PENSIEVE_IT_S3_SECRET_KEY=... uv run --extra capture pytest -q \
      tests/test_archive_integration.py
"""

import gzip
import os

import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.archive import capture, save
from pensieve.archive import render as render_mod
from pensieve.archive import storage as storage_mod
from tests.test_fetch_helpers import fake_queue  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("PENSIEVE_IT") != "1", reason="integration: set PENSIEVE_IT=1")


@pytest.fixture
def real_stack():
    store = storage_mod.S3Storage(
        os.environ.get("PENSIEVE_IT_S3_ENDPOINT", "http://localhost:3900"),
        os.environ.get("PENSIEVE_IT_S3_BUCKET", "pensieve-archive"),
        os.environ["PENSIEVE_IT_S3_ACCESS_KEY"],
        os.environ["PENSIEVE_IT_S3_SECRET_KEY"],
        "garage",
    )
    storage_mod.set_storage(store)
    render_mod.set_renderer(
        render_mod.PlaywrightRenderer(os.environ.get("PENSIEVE_IT_BROWSER", "ws://localhost:3000/"))
    )
    yield store
    storage_mod.reset_storage()
    render_mod.set_renderer(None)


async def test_real_capture_of_a_public_page(session, user, fake_queue, real_stack):
    result = await save.save_link(session, user, os.environ.get("PENSIEVE_IT_URL", "https://example.com/"))
    snap = await capture.capture_snapshot(session, result.snapshot.id)
    assert snap.status == "done" and snap.render_mode == "browser", snap.error
    page = gzip.decompress(await real_stack.get(snap.page_key)).decode()
    assert "<script" not in page and "Archived copy of" in page
    shot = await real_stack.get(snap.shot_key)
    assert shot[:3] == b"\xff\xd8\xff"
    item = await session.get(models.Item, result.item.id)
    assert item.content_text and item.title != item.url
    assets = (
        await session.scalars(
            select(models.SnapshotAsset.sha256).where(models.SnapshotAsset.snapshot_id == snap.id)
        )
    ).all()
    for sha in assets:
        assert await real_stack.exists(storage_mod.asset_key(sha))


async def test_browser_refuses_private_addresses(real_stack):
    """A page that tries to load a LAN address from inside Chromium gets it blocked by the route guard."""
    renderer = render_mod.PlaywrightRenderer(os.environ.get("PENSIEVE_IT_BROWSER", "ws://localhost:3000/"))
    with pytest.raises(render_mod.RenderError):
        await renderer.render("http://10.0.0.1/")
