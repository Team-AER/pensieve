import uuid

from sqlalchemy import select

from pensieve import models
from pensieve.auth import hash_api_token
from tests.test_web_support import fake_module, login, make_user, seed_feed


async def test_add_feed_uses_fetch_package(client, session, user, monkeypatch):
    folder = models.Folder(user_id=user.id, name="News")
    session.add(folder)
    await session.commit()
    seen = []

    class FeedError(Exception):
        pass

    async def add_feed(session_, user_, url, folder_id=None):
        seen.append((url, folder_id))
        if "bad" in url:
            raise FeedError("No feed found at that address")
        feed = models.Feed(user_id=user_.id, url=url, title="Added", folder_id=folder_id)
        session_.add(feed)
        return feed

    fake_module(monkeypatch, "pensieve.fetch.subscribe", add_feed=add_feed, FeedError=FeedError)
    headers = await login(client, user)
    r = await client.post("/manage/feeds", data={"url": "https://good.example/feed", "folder_id": str(folder.id)}, headers=headers)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=feed_added")
    assert seen == [("https://good.example/feed", folder.id)]
    feed = await session.scalar(select(models.Feed).where(models.Feed.url == "https://good.example/feed"))
    assert feed is not None and feed.folder_id == folder.id
    r = await client.post("/manage/feeds", data={"url": "https://bad.example/"}, headers=headers)
    assert r.status_code == 400 and "No feed found at that address" in r.text
    r = await client.get("/manage/feeds")
    assert r.status_code == 200 and "Added" in r.text and "News" in r.text


async def test_feed_actions(client, session, user):
    folder = models.Folder(user_id=user.id, name="Later")
    session.add(folder)
    await session.commit()
    feed = await seed_feed(session, user, "Old name")
    headers = await login(client, user)
    await client.post(f"/manage/feeds/{feed.id}/rename", data={"title": "New name"}, headers=headers)
    await client.post(f"/manage/feeds/{feed.id}/move", data={"folder_id": str(folder.id)}, headers=headers)
    await client.post(f"/manage/feeds/{feed.id}/pause", headers=headers)
    await session.refresh(feed)
    assert feed.title == "New name" and feed.folder_id == folder.id and feed.paused
    await client.post(f"/manage/feeds/{feed.id}/resume", headers=headers)
    await session.refresh(feed)
    assert not feed.paused
    r = await client.post(f"/manage/feeds/{feed.id}/unsubscribe", headers=headers)
    assert r.status_code == 303
    fid = feed.id
    session.expire(feed)
    assert await session.get(models.Feed, fid) is None


async def test_accept_ai_folder_suggestion(client, session, user, monkeypatch):
    suggested = models.Folder(user_id=user.id, name="Suggested", ai_suggested=True)
    other = models.Folder(user_id=user.id, name="Other")
    session.add_all([suggested, other])
    await session.commit()
    feed = await seed_feed(session, user, "Inbox feed", suggested_folder_id=suggested.id, suggested_folder_confidence=0.8)
    corrections = []

    async def record_correction(session_, user_, target_type, target_id, field, old, new):
        corrections.append((target_type, target_id, field, old, new))

    fake_module(monkeypatch, "pensieve.ai.service", record_correction=record_correction)
    headers = await login(client, user)
    r = await client.get("/manage/feeds?folder=inbox")
    assert "Suggested · suggested" in r.text and "Accept" in r.text
    r = await client.post(f"/manage/feeds/{feed.id}/accept-suggestion", headers=headers)
    assert r.status_code == 303
    await session.refresh(feed)
    assert feed.folder_id == suggested.id and corrections == []
    # Picking a different folder records a correction.
    feed.folder_id = None
    await session.commit()
    await client.post(f"/manage/feeds/{feed.id}/accept-suggestion", data={"folder_id": str(other.id)}, headers=headers)
    await session.refresh(feed)
    assert feed.folder_id == other.id
    assert corrections == [("feed_folder", feed.id, "folder_id", str(suggested.id), str(other.id))]


async def test_folder_crud_and_reorder(client, session, user):
    headers = await login(client, user)
    r = await client.post("/manage/folders", data={"name": "Alpha"}, headers=headers)
    assert r.status_code == 303
    await client.post("/manage/folders", data={"name": "Beta"}, headers=headers)
    folders = {f.name: f for f in await session.scalars(select(models.Folder).where(models.Folder.user_id == user.id))}
    assert set(folders) == {"Alpha", "Beta"}
    r = await client.post("/manage/folders", data={"name": "Alpha"}, headers=headers)
    assert "already+exists" in r.headers["location"]
    await client.post(f"/manage/folders/{folders['Alpha'].id}/rename", data={"name": "Alpha2"}, headers=headers)
    await session.refresh(folders["Alpha"])
    assert folders["Alpha"].name == "Alpha2"
    await client.post("/manage/folders/reorder", data={"order": f"{folders['Beta'].id},{folders['Alpha'].id}"}, headers=headers)
    await session.refresh(folders["Beta"])
    await session.refresh(folders["Alpha"])
    assert folders["Beta"].position == 0 and folders["Alpha"].position == 1
    r = await client.get("/manage/folders")
    assert r.status_code == 200 and r.text.index("Beta") < r.text.index("Alpha2")
    await client.post(f"/manage/folders/{folders['Beta'].id}/delete", headers=headers)
    bid = folders["Beta"].id
    session.expire(folders["Beta"])
    assert await session.get(models.Folder, bid) is None
    # Another user's folder is not reachable.
    other = await make_user(session)
    theirs = models.Folder(user_id=other.id, name="Theirs")
    session.add(theirs)
    await session.commit()
    r = await client.post(f"/manage/folders/{theirs.id}/delete", headers=headers)
    assert r.status_code == 404


async def test_tags_and_rules(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    headers = await login(client, user)
    await client.post("/manage/tags", data={"name": "kubernetes", "kind": "ai", "description": "Container orchestration"}, headers=headers)
    tag = await session.scalar(select(models.Tag).where(models.Tag.user_id == user.id))
    assert tag.kind == "ai" and tag.description == "Container orchestration"
    await client.post(f"/manage/tags/{tag.id}/update", data={"name": "k8s", "kind": "user", "description": ""}, headers=headers)
    await session.refresh(tag)
    assert tag.name == "k8s" and tag.kind == "user"
    await client.post(f"/manage/tags/{tag.id}/delete", headers=headers)
    tid = tag.id
    session.expire(tag)
    assert await session.get(models.Tag, tid) is None
    r = await client.post("/manage/rules", data={"feed_id": str(feed.id), "field": "title", "pattern": "sponsored", "action": "hide"}, headers=headers)
    assert r.status_code == 303
    rule = await session.scalar(select(models.FeedRule).where(models.FeedRule.user_id == user.id))
    assert rule.feed_id == feed.id and rule.action == "hide" and rule.enabled and not rule.is_regex
    await client.post(f"/manage/rules/{rule.id}/update", data={"feed_id": "", "field": "body", "pattern": "^Ad:", "is_regex": "1", "action": "tag", "action_value": "ads"}, headers=headers)
    await session.refresh(rule)
    assert rule.feed_id is None and rule.is_regex and rule.action == "tag" and rule.action_value == "ads"
    await client.post(f"/manage/rules/{rule.id}/toggle", headers=headers)
    await session.refresh(rule)
    assert not rule.enabled
    r = await client.get("/manage/rules")
    assert r.status_code == 200 and "^Ad:" in r.text
    await client.post(f"/manage/rules/{rule.id}/delete", headers=headers)
    rid = rule.id
    session.expire(rule)
    assert await session.get(models.FeedRule, rid) is None


async def test_ai_settings_and_profile(client, session, user):
    headers = await login(client, user)
    r = await client.get("/manage/ai")
    assert r.status_code == 200 and "Reader profile" in r.text and "/manage/ai/gateway" in r.text
    r = await client.post("/manage/ai/settings", data={"auto_file": "1", "digest": "1", "digest_time": "08:15"}, headers=headers)
    assert r.status_code == 303
    await session.refresh(user)
    assert user.settings["auto_file"] is True and user.settings["tag_items"] is False and user.settings["digest_time"] == "08:15"
    await client.post("/manage/ai/profile", data={"body_text": "Likes databases."}, headers=headers)
    await client.post("/manage/ai/profile", data={"body_text": "Likes databases and Rust."}, headers=headers)
    profiles = list(await session.scalars(select(models.Profile).where(models.Profile.user_id == user.id).order_by(models.Profile.version)))
    assert [p.version for p in profiles] == [1, 2] and all(p.edited_by_user for p in profiles)
    r = await client.get("/manage/ai")
    assert "Likes databases and Rust." in r.text and "v2" in r.text


async def test_token_create_and_revoke(client, session, user):
    headers = await login(client, user)
    r = await client.post("/manage/tokens", data={"label": "Reeder", "kind": "greader"}, headers=headers)
    assert r.status_code == 200 and "Copy it now" in r.text
    token = await session.scalar(select(models.ApiToken).where(models.ApiToken.user_id == user.id))
    assert token.label == "Reeder" and token.kind == "greader"
    import re

    plaintext = re.search(r'id="new-token">([^<]+)<', r.text).group(1)
    assert hash_api_token(plaintext) == token.token_hash
    r = await client.get("/manage/tokens")
    assert plaintext not in r.text and "Reeder" in r.text
    r = await client.post(f"/manage/tokens/{token.id}/revoke", headers=headers)
    assert r.status_code == 303
    await session.refresh(token)
    assert token.revoked_at is not None
    # Someone else's token cannot be revoked.
    other = await make_user(session)
    theirs = models.ApiToken(user_id=other.id, label="x", token_hash=hash_api_token(uuid.uuid4().hex))
    session.add(theirs)
    await session.commit()
    r = await client.post(f"/manage/tokens/{theirs.id}/revoke", headers=headers)
    assert r.status_code == 404


async def test_household_users_admin_only(client, session, user):
    reader = await make_user(session, role=models.UserRole.reader)
    headers = await login(client, reader)
    r = await client.get("/manage/users")
    assert r.status_code == 403
    r = await client.post("/manage/users", data={"email": "x@example.com"}, headers=headers)
    assert r.status_code == 403
    await client.post("/logout")
    headers = await login(client, user)  # admin
    r = await client.get("/manage/users")
    assert r.status_code == 200 and reader.email in r.text
    r = await client.post("/manage/users", data={"email": "new@example.com", "display_name": "New", "role": "reader"}, headers=headers)
    assert r.status_code == 200 and "temporary password" in r.text
    new = await session.scalar(select(models.User).where(models.User.email == "new@example.com"))
    assert new is not None and new.role == models.UserRole.reader
    await client.post(f"/manage/users/{new.id}/role", headers=headers)
    await client.post(f"/manage/users/{new.id}/ai", headers=headers)
    await session.refresh(new)
    assert new.role == models.UserRole.admin and new.ai_enabled is False
    r = await client.post(f"/manage/users/{user.id}/delete", headers=headers)
    assert "yourself" in r.headers["location"]
    await client.post(f"/manage/users/{new.id}/delete", headers=headers)
    nid = new.id
    session.expire(new)
    assert await session.get(models.User, nid) is None


async def test_account_profile_and_password(client, session, user):
    headers = await login(client, user)
    await client.post("/manage/account/profile", data={"display_name": "Renamed", "theme": "sepia"}, headers=headers)
    await session.refresh(user)
    assert user.display_name == "Renamed" and user.settings["theme"] == "sepia"
    r = await client.get("/manage/account")
    assert 'data-theme="sepia"' in r.text
    r = await client.post("/manage/account/password", data={"current_password": "wrong", "new_password": "newpassword1", "confirm_password": "newpassword1"}, headers=headers)
    assert "wrong" in r.headers["location"]
    r = await client.post("/manage/account/password", data={"current_password": "password123", "new_password": "newpassword1", "confirm_password": "newpassword1"}, headers=headers)
    assert r.headers["location"].endswith("password_changed")
    await client.post("/logout")
    await login(client, user, password="newpassword1")


async def test_opml_import_and_export(client, session, user, monkeypatch):
    class ImportResult:
        added = 3
        skipped = 1
        folders_created = 2

    received = []

    async def import_opml(session_, user_, xml):
        received.append(xml)
        return ImportResult()

    def export_opml(user_, feeds, folders):
        return b"<opml/>"

    fake_module(monkeypatch, "pensieve.fetch.opml", import_opml=import_opml, export_opml=export_opml)
    headers = await login(client, user)
    r = await client.post("/manage/import/opml", files={"file": ("subs.opml", b"<opml><body/></opml>", "text/xml")}, headers=headers)
    assert r.status_code == 200 and "Added 3 feeds, skipped 1, created 2 folders" in r.text
    assert received == [b"<opml><body/></opml>"]
    r = await client.get("/manage/export/opml")
    assert r.status_code == 200 and r.content == b"<opml/>" and "attachment" in r.headers["content-disposition"]
    await seed_feed(session, user, "Exported")
    r = await client.get("/manage/export/json")
    assert r.status_code == 200
    data = r.json()
    assert data["feeds"][0]["title"] == "Exported" and data["items"] == [] and data["profile"] is None
