# ruff: noqa: F811  (fixtures imported from test_web_support are used as test parameters)
from sqlalchemy import select

from pensieve import models
from pensieve.web.templating import make_csrf
from tests.test_web_support import HTML, login, login_form, make_user, memory_limiter, seed_feed  # noqa: F401


async def test_setup_creates_first_admin(client, session):
    r = await client.get("/", headers=HTML)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = await client.get("/setup")
    assert r.status_code == 200 and "Create your account" in r.text
    r = await client.post(
        "/setup",
        data={
            "email": "Admin@Example.com",
            "display_name": "Admin",
            "password": "hunter2hunter2",
            "password_confirm": "hunter2hunter2",
        },
    )
    # A brand-new install has no feeds yet: start where feeds are added, not on an empty Reader.
    assert r.status_code == 303 and r.headers["location"] == "/manage/feeds?msg=welcome"
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
    r = await client.post("/login", data=login_form(user.email, "wrong"))
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


async def test_next_only_relative(client, session, user):
    await seed_feed(session, user)
    r = await client.post("/login", data=login_form(user.email, "password123", next="https://evil.example"))
    assert r.headers["location"] == "/"
    r = await client.post("/login", data=login_form(user.email, "password123", next="//evil.example"))
    assert r.headers["location"] == "/"
    r = await client.post("/login", data=login_form(user.email, "password123", next="/reader/starred"))
    assert r.headers["location"] == "/reader/starred"


async def test_unauthenticated_redirects_to_login(client, user):
    r = await client.get("/reader/starred", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/reader/starred"
    r = await client.get("/manage/feeds", headers=HTML)
    assert r.status_code == 303


async def test_logout_clears_session(client, user):
    headers = await login(client, user)
    # Logout needs the CSRF token too (a third-party page must not be able to sign the user out).
    r = await client.post("/logout")
    assert r.status_code == 403
    r = await client.post("/logout", headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.get("/", headers=HTML)
    assert r.status_code == 303


async def test_login_requires_anonymous_csrf_token(client, user):
    r = await client.post("/login", data={"email": user.email, "password": "password123"})
    assert r.status_code == 403 and "pensieve_session" not in r.cookies
    r = await client.get("/login")
    assert 'name="csrf_token"' in r.text


async def test_login_rate_limit_blocks_after_failures(client, user, memory_limiter):
    memory_limiter.attempts = 3
    for _ in range(3):
        r = await client.post("/login", data=login_form(user.email, "wrong"))
        assert r.status_code == 401
    r = await client.post("/login", data=login_form(user.email, "wrong"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    # Even the right password is refused while blocked; another email is unaffected.
    r = await client.post("/login", data=login_form(user.email, "password123"))
    assert r.status_code == 429
    r = await client.post("/login", data=login_form("someone-else@example.com", "wrong"))
    assert r.status_code == 401
    await memory_limiter.reset(("127.0.0.1", user.email))
    memory_limiter._hits.clear()
    r = await client.post("/login", data=login_form(user.email, "password123"))
    assert r.status_code == 303
    # Success resets the counter: three more wrong tries are needed before blocking again.
    await client.post("/logout", headers={"X-CSRF-Token": make_csrf(user.id)})
    for _ in range(3):
        assert (await client.post("/login", data=login_form(user.email, "wrong"))).status_code == 401
    assert (await client.post("/login", data=login_form(user.email, "wrong"))).status_code == 429


async def test_security_headers_present(client, user):
    r = await client.get("/login")
    csp = r.headers["content-security-policy"]
    assert (
        "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "frame-src 'self' https:" in csp
    )
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers  # session_cookie_secure is False on the LAN
    assert r.headers["content-security-policy"] == csp


async def test_post_without_csrf_is_rejected(client, session, user):
    await login(client, user)
    r = await client.post("/manage/folders", data={"name": "Nope"})
    assert r.status_code == 403


async def test_first_sign_in_lands_somewhere_the_account_can_open(client, session):
    """A reader handed the admin's Household users link (plus a temporary password) must not land on a 403."""
    user = await make_user(session)  # a reader, as invited from Household users
    r = await client.post("/login", data=login_form(user.email, "password123", next="/manage/users"))
    assert r.status_code == 303 and r.headers["location"] == "/manage/feeds?msg=welcome"
    r = await client.get(r.headers["location"], headers=HTML)
    assert r.status_code == 200 and "Welcome to Pensieve" in r.text
    # With a feed, the same link goes to the Reader; an allowed page is honoured as before.
    await seed_feed(session, user)
    r = await client.post("/login", data=login_form(user.email, "password123", next="/manage/users?x=1"))
    assert r.headers["location"] == "/"
    r = await client.post("/login", data=login_form(user.email, "password123", next="/manage/feeds"))
    assert r.headers["location"] == "/manage/feeds"
    # Already signed in: GET /login?next=... takes the same route.
    r = await client.get("/login?next=/manage/users")
    assert r.status_code == 303 and r.headers["location"] == "/"
    # ...and so does opening the admin page itself while signed in as a reader.
    r = await client.get("/manage/users", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/"


async def test_admin_keeps_admin_next_and_invite_shows_sign_in_link(client, session):
    admin = await make_user(session, models.UserRole.admin)
    await seed_feed(session, admin)
    r = await client.post("/login", data=login_form(admin.email, "password123", next="/manage/users"))
    assert r.headers["location"] == "/manage/users"
    headers = await login(client, admin)
    r = await client.post(
        "/manage/users", data={"email": "new@example.com", "role": "reader"}, headers=headers
    )
    assert r.status_code == 200 and "Sign-in link" in r.text and "http://test/login" in r.text
