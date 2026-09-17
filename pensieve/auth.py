"""Authentication primitives shared by the web UI and the sync APIs.

- Passwords: argon2id.
- Web sessions: signed cookie (itsdangerous) holding the user id.
- API tokens: random 40-char secret, stored as SHA-256; presented by sync clients.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve.config import get_settings
from pensieve.db import get_session
from pensieve.models import ApiToken, User

SESSION_COOKIE = "pensieve_session"
SESSION_MAX_AGE_S = 60 * 60 * 24 * 30

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="session")


def make_session_cookie(user_id: uuid.UUID) -> str:
    return _serializer().dumps(str(user_id))


def read_session_cookie(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(_serializer().loads(value, max_age=SESSION_MAX_AGE_S))
    except (BadSignature, ValueError):
        return None


def generate_api_token() -> str:
    return secrets.token_urlsafe(30)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def user_from_session(request: Request, session: AsyncSession) -> User | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    user_id = read_session_cookie(raw)
    if user_id is None:
        return None
    return await session.get(User, user_id)


async def user_from_api_token(token: str, session: AsyncSession, touch: bool = True) -> User | None:
    row = await session.scalar(
        select(ApiToken).where(ApiToken.token_hash == hash_api_token(token), ApiToken.revoked_at.is_(None))
    )
    if row is None:
        return None
    if touch:
        row.last_used_at = datetime.now(UTC)
    return await session.get(User, row.user_id)


async def current_user_optional(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    return await user_from_session(request, session)


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    """Web dependency: session cookie required. Raises 401 (the web layer redirects to /login)."""
    user = await user_from_session(request, session)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if user.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
