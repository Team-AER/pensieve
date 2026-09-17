"""Login, logout and first-run setup."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    hash_password,
    make_session_cookie,
    user_from_session,
    verify_password,
)
from pensieve.config import get_settings
from pensieve.models import User, UserRole
from pensieve.web.ratelimit import client_ip, get_limiter
from pensieve.web.templating import DB, CsrfUser, check_csrf, render

router = APIRouter()


def safe_next(value: str | None) -> str:
    """Only relative paths are honoured for post-login redirects."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def set_session(response: Response, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(user.id),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        path="/",
    )


async def user_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(User.id))) or 0)


@router.get("/login")
async def login_page(request: Request, session: DB, next: str = "/"):
    if await user_count(session) == 0:
        return RedirectResponse("/setup", status_code=303)
    if await user_from_session(request, session) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next), "error": None, "email": ""})


@router.post("/login")
async def login_submit(
    request: Request,
    session: DB,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
    csrf_token: Annotated[str, Form()] = "",
):
    email = email.strip().lower()

    def fail(message: str, status_code: int, headers: dict[str, str] | None = None):
        return render(
            request,
            "login.html",
            {"next": safe_next(next), "error": message, "email": email},
            status_code=status_code,
            headers=headers,
        )

    # The login form carries an anonymous CSRF token (subject "anon") so a third-party page cannot log the
    # browser into an attacker-chosen account (login CSRF).
    if not check_csrf(csrf_token, None):
        return fail("Your session expired. Please try again.", 403)
    limiter = get_limiter()
    key = (client_ip(request), email)
    retry_after = await limiter.blocked_for(key)
    if retry_after:
        return fail("Too many failed sign-ins. Try again later.", 429, {"Retry-After": str(retry_after)})
    user = await session.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or not verify_password(password, user.password_hash):
        await limiter.record_failure(key)
        return fail("That email and password don't match.", 401)
    await limiter.reset(key)
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session(response, user)
    return response


@router.post("/logout")
async def logout(user: CsrfUser):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/setup")
async def setup_page(request: Request, session: DB):
    if await user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", {"error": None, "email": "", "display_name": ""})


@router.post("/setup")
async def setup_submit(
    request: Request,
    session: DB,
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    if await user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    email = email.strip().lower()
    error = None
    if "@" not in email:
        error = "Enter a valid email address."
    elif len(password) < 8:
        error = "Use a password of at least 8 characters."
    elif password != password_confirm:
        error = "The passwords don't match."
    if error:
        return render(
            request,
            "setup.html",
            {"error": error, "email": email, "display_name": display_name},
            status_code=400,
        )
    user = User(
        email=email,
        display_name=display_name.strip() or email.split("@")[0],
        password_hash=hash_password(password),
        role=UserRole.admin,
        settings={},
    )
    session.add(user)
    await session.commit()
    response = RedirectResponse("/", status_code=303)
    set_session(response, user)
    return response
