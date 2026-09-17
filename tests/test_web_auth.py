from sqlalchemy import select

from pensieve import models
from tests.test_web_support import HTML, login


async def test_setup_creates_first_admin(client, session):
    r = await client.get("/", headers=HTML)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = await client.get("/setup")
    assert r.status_code == 200 and "Create your account" in r.text
    r = await client.post(
        "/setup",
        data={"email": "Admin@Example.com", "display_name": "Admin", "password": "hunter2hunter2", "password_confirm": "hunter2hunter2"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "pensieve_session" in r.cookies
    admin = await session.scalar(select(models.User).where(models.User.email == "admin@example.com"))
    assert admin is not None and admin.role == models.UserRole.admin
    # Setup is one-shot.
    r = await client.get("/setup")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_setup_validates(client, session):
    r = await client.post("/setup", data={"email": "nope", "password": "short", "password_confirm": "short"})
    assert r.status_code == 400 and "valid email" in r.text


async def test_login_sets_cookie_and_wrong_password_fails(client, user):
    r = await client.post("/login", data={"email": user.email, "password": "wrong"})
    assert r.status_code == 401 and "email and password don" in r.text
    assert "pensieve_session" not in r.cookies
    headers = await login(client, user)
    assert headers["X-CSRF-Token"]
    r = await client.get("/")
    assert r.status_code == 200 and "All unread" in r.text


async def test_login_page_renders_split_layout(client, user):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "Your feeds, sorted, grouped and remembered." in r.text


async def test_next_only_relative(client, user):
    r = await client.post("/login", data={"email": user.email, "password": "password123", "next": "https://evil.example"})
    assert r.headers["location"] == "/"
    r = await client.post("/login", data={"email": user.email, "password": "password123", "next": "//evil.example"})
    assert r.headers["location"] == "/"
    r = await client.post("/login", data={"email": user.email, "password": "password123", "next": "/reader/starred"})
    assert r.headers["location"] == "/reader/starred"


async def test_unauthenticated_redirects_to_login(client, user):
    r = await client.get("/reader/starred", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/reader/starred"
    r = await client.get("/manage/feeds", headers=HTML)
    assert r.status_code == 303


async def test_logout_clears_session(client, user):
    await login(client, user)
    r = await client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.get("/", headers=HTML)
    assert r.status_code == 303


async def test_post_without_csrf_is_rejected(client, session, user):
    await login(client, user)
    r = await client.post("/manage/folders", data={"name": "Nope"})
    assert r.status_code == 403
