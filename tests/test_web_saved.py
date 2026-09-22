# ruff: noqa: F811
"""Saved links in the web UI and API: saving, the Saved views, archive serving and its security headers."""

import gzip
from datetime import UTC, datetime

from sqlalchemy import select

from pensieve import models, queue
from pensieve.archive import save
from pensieve.archive.storage import asset_key
from pensieve.auth import generate_api_token, hash_api_token
from tests.test_archive_helpers import PNG, bucket, no_bucket  # noqa: F401
from tests.test_fetch_helpers import fake_queue, no_dns  # noqa: F401
from tests.test_web_support import (  # noqa: F401
    HTML,
    HX,
    login,
    make_user,
    memory_limiter,
    seed_feed,
    seed_item,
)


async def _done_snapshot(session, bucket, item, user, *, page=b"<html><body><p>frozen</p></body></html>"):
    snap = await session.scalar(select(models.Snapshot).where(models.Snapshot.item_id == item.id))
    sha = "a" * 64
    bucket.objects[f"snap/{snap.id}/1/page.html.gz"] = (gzip.compress(page), "text/html", "gzip")
    bucket.objects[f"snap/{snap.id}/1/shot.jpg"] = (b"\xff\xd8shot", "image/jpeg", None)
    bucket.objects[asset_key(sha)] = (PNG, "image/png", None)
    session.add(models.ArchiveAsset(sha256=sha, content_type="image/png", size=len(PNG)))
    await session.flush()
    session.add(models.SnapshotAsset(snapshot_id=snap.id, sha256=sha))
    snap.status, snap.generation, snap.captured_at = "done", 1, datetime.now(UTC)
    snap.page_key, snap.shot_key, snap.render_mode = f"snap/{snap.id}/1/page.html.gz", f"snap/{snap.id}/1/shot.jpg", "browser"
    item.content_html = f'<p>The archived article.</p><p><img src="/archive/a/{sha}"></p>'
    item.content_text = "The archived article."
    await session.commit()
    return snap, sha


async def test_save_dialog_saves_and_lists_under_saved_not_unread(client, session, user, fake_queue):
    feed = await seed_feed(session, user, "Feed")
    await seed_item(session, feed, "A feed item")
    headers = await login(client, user)
    r = await client.post("/saved", data={"url": "https://example.com/post?utm_source=tw", "tags": "Later, rust"}, headers=headers | HX)
    assert r.status_code == 200 and "Saved" in r.text and "link-saved" in r.headers["HX-Trigger"]
    assert any(c[0] == queue.CAPTURE_PAGE for c in fake_queue)
    item = await session.scalar(select(models.Item).where(models.Item.url == "https://example.com/post"))
    assert item is not None
    r = await client.get("/reader/saved/list", headers=HX)
    assert "example.com" in r.text and "A feed item" not in r.text
    r = await client.get("/reader/unread/list", headers=HX)
    assert "A feed item" in r.text and "https://example.com/post" not in r.text
    r = await client.get("/reader/saved/archive/list", headers=HX)
    assert "Nothing archived yet" in r.text
    r = await client.get("/reader/nav?view=saved", headers=HX)
    assert "Saved" in r.text and 'href="/reader/saved"' in r.text
    # Errors come back in the dialog, not as a failed request.
    r = await client.post("/saved", data={"url": "ftp://x.org/f"}, headers=headers | HX)
    assert r.status_code == 200 and "flash-error" in r.text
    # CSRF is required.
    r = await client.post("/saved", data={"url": "https://example.com/x"}, headers=HX)
    assert r.status_code == 403


async def test_bookmarklet_page_confirms_before_saving(client, session, user, fake_queue):
    await login(client, user)
    r = await client.get("/save", params={"text": "Worth a read https://blog.example.com/p/1", "title": "Blog"}, headers=HTML)
    assert r.status_code == 200 and 'value="https://blog.example.com/p/1"' in r.text and "Save to Pensieve" in r.text
    assert await session.scalar(select(models.Item.id).where(models.Item.url == "https://blog.example.com/p/1")) is None
    from pensieve.web.templating import make_csrf

    r = await client.post("/save", data={"url": "https://blog.example.com/p/1", "csrf_token": make_csrf(user.id)}, headers=HTML)
    assert r.status_code == 200 and "Saved" in r.text and "Read it now" in r.text
    assert await session.scalar(select(models.Item.id).where(models.Item.url == "https://blog.example.com/p/1"))


async def test_login_redirect_keeps_the_shared_link(client):
    r = await client.get("/save?url=https%3A%2F%2Fa.org%2Fx&title=T", headers=HTML)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=/save%3Furl%3Dhttps")


async def test_token_api_saves_and_rejects_bad_tokens(client, session, user, fake_queue):
    token = generate_api_token()
    session.add(models.ApiToken(user_id=user.id, label="phone", kind="web", token_hash=hash_api_token(token)))
    await session.commit()
    r = await client.post("/api/v1/save", json={"url": "https://news.example.com/a", "tags": ["x"]}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201 and r.json()["created"] is True and r.json()["status"] == "queued"
    r = await client.post("/api/v1/save", data={"url": "https://news.example.com/a"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["created"] is False
    r = await client.post("/api/v1/save", json={"url": "https://news.example.com/b", "html": "<html><body>mine</body></html>"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    assert any(c[0] == queue.CAPTURE_PAGE and c[1] for c in fake_queue)
    r = await client.post("/api/v1/save", json={"url": "https://news.example.com/c"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.post("/api/v1/save", json={"url": "javascript:alert(1)"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422


async def test_archive_views_serve_with_locked_down_headers(client, session, user, fake_queue, bucket):
    result = await save.save_link(session, user, "https://news.example.com/story")
    snap, sha = await _done_snapshot(session, bucket, result.item, user)
    await login(client, user)
    r = await client.get(f"/items/{result.item.id}", headers=HX)
    assert r.status_code == 200 and "Archived" in r.text and "/view/page" in r.text and "Screenshot" in r.text
    assert "Reader view</span>" not in r.text  # the saved article already is the reader view
    r = await client.get(f"/items/{result.item.id}/view/page", headers=HX)
    assert f'src="/archive/s/{snap.id}/page?g=1"' in r.text and 'sandbox="allow-same-origin' in r.text
    assert "allow-scripts" not in r.text
    r = await client.get(f"/archive/s/{snap.id}/page", headers={"Accept-Encoding": "identity"})
    csp = r.headers["content-security-policy"]
    assert r.status_code == 200 and b"frozen" in r.content
    assert "default-src 'none'" in csp and "sandbox" in csp and "script-src" not in csp and "allow-scripts" not in csp
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    r = await client.get(f"/archive/s/{snap.id}/shot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "sandbox" in r.headers["content-security-policy"]
    r = await client.get(f"/archive/a/{sha}")
    assert r.status_code == 200 and r.content == PNG and "immutable" in r.headers["cache-control"]
    r = await client.get("/archive/a/" + "b" * 64)
    assert r.status_code == 404
    # Another user can't reach any of it.
    other = await make_user(session)
    client.cookies.clear()
    await login(client, other)
    for path in (f"/archive/s/{snap.id}/page", f"/archive/s/{snap.id}/shot", f"/archive/a/{sha}", f"/items/{result.item.id}/view/page"):
        assert (await client.get(path)).status_code == 404, path


async def test_capture_poll_and_recapture(client, session, user, fake_queue, bucket):
    result = await save.save_link(session, user, "https://news.example.com/story")
    headers = await login(client, user)
    r = await client.get(f"/items/{result.item.id}/capture", headers=HX)
    assert r.status_code == 204
    r = await client.get(f"/items/{result.item.id}", headers=HX)
    assert "Waiting to capture the page" in r.text and f"/items/{result.item.id}/capture" in r.text
    await _done_snapshot(session, bucket, result.item, user)
    r = await client.get(f"/items/{result.item.id}/capture", headers=HX)
    assert r.status_code == 200 and r.headers["HX-Retarget"] == "#article" and "The archived article." in r.text
    fake_queue.clear()
    r = await client.post(f"/items/{result.item.id}/recapture", headers=headers | HX)
    assert r.status_code == 200 and "Waiting to capture" in r.text
    assert [c[0] for c in fake_queue] == [queue.CAPTURE_PAGE]


async def test_feed_item_save_link_and_star_archives(client, session, user, fake_queue):
    feed = await seed_feed(session, user, "Feed")
    item = await seed_item(session, feed, "Feed story", url="https://news.example.com/feed-story")
    headers = await login(client, user)
    r = await client.post(f"/items/{item.id}/star", headers=headers | HX)
    assert r.status_code == 200
    snap = await session.scalar(select(models.Snapshot).where(models.Snapshot.item_id == item.id))
    assert snap is not None and snap.status == "queued"  # starring keeps a copy
    r = await client.post(f"/items/{item.id}/save-link", headers=headers | HX)
    assert r.status_code == 204 and "link-saved" in r.headers["HX-Trigger"]
    saved = await session.scalar(
        select(models.Item).join(models.Feed).where(models.Feed.kind == "saved", models.Item.url == item.url)
    )
    assert saved is not None and saved.title == "Feed story"


async def test_saving_page_token_and_import(client, session, user, fake_queue, no_bucket):
    headers = await login(client, user)
    r = await client.get("/manage/saving", headers=HTML)
    assert r.status_code == 200 and "Bookmarklet" in r.text and "javascript:" in r.text and "Text only" in r.text
    r = await client.post("/manage/saving/token", data={"label": "iPhone"}, headers=headers | HTML)
    assert "Bearer " in r.text and "/api/v1/save" in r.text
    csv = b"title,url,time_added,tags,status\nOne,https://b.org/1,1700000000,a|b,unread\n"
    r = await client.post("/manage/saving/import", files={"file": ("pocket.csv", csv, "text/csv")}, headers=headers)
    assert r.status_code == 303 and "import_started" in r.headers["location"]
    assert fake_queue[-1][0] == queue.CAPTURE_IMPORT and fake_queue[-1][1][1][0]["url"] == "https://b.org/1"
    r = await client.post("/manage/saving/import", files={"file": ("x.txt", b"nothing", "text/plain")}, headers=headers)
    assert "No links found" in r.text
    r = await client.post("/manage/saving/settings", data={}, headers=headers)
    await session.refresh(user)
    assert r.status_code == 303 and user.settings.get("archive_starred") is False


async def test_import_job_saves_links_without_capturing(session, user, fake_queue):
    from pensieve.archive.jobs import import_links

    links = [
        {"url": "https://b.org/1", "title": "One", "tags": ["a"], "saved_at": 1700000000, "read": False, "starred": False},
        {"url": "https://b.org/2", "title": "Two", "tags": [], "saved_at": 1600000000, "read": True, "starred": True},
        {"url": "not a url at all", "title": "", "tags": [], "saved_at": None, "read": False, "starred": False},
    ]
    assert await import_links({}, str(user.id), links) == 2
    items = (await session.scalars(select(models.Item).join(models.Feed).where(models.Feed.kind == "saved"))).all()
    by_url = {i.url: i for i in items}
    assert set(by_url) >= {"https://b.org/1", "https://b.org/2"}
    assert by_url["https://b.org/2"].published_at.year == 2020
    state = await session.get(models.ItemState, (user.id, by_url["https://b.org/2"].id))
    assert state.is_read and state.is_starred
    assert not any(c[0] == queue.CAPTURE_PAGE for c in fake_queue)  # the sweep trickles them in


async def test_sync_clients_cannot_unsubscribe_saved(client, session, user, fake_queue):
    await save.save_link(session, user, "https://news.example.com/keep")
    feed = await session.scalar(select(models.Feed).where(models.Feed.kind == "saved"))
    token = generate_api_token()
    session.add(models.ApiToken(user_id=user.id, label="reeder", kind="greader", token_hash=hash_api_token(token)))
    await session.commit()
    r = await client.post(
        "/reader/api/0/subscription/edit",
        data={"ac": "unsubscribe", "s": f"feed/{feed.id}"},
        headers={"Authorization": f"GoogleLogin auth={token}"},
    )
    assert r.status_code == 200
    assert await session.scalar(select(models.Feed.id).where(models.Feed.kind == "saved")) == feed.id
