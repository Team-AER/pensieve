from sqlalchemy import select

from pensieve import models
from pensieve.auth import generate_api_token, hash_api_token, user_from_api_token, verify_password


async def test_user_roundtrip(session, user):
    row = await session.scalar(select(models.User).where(models.User.id == user.id))
    assert row is not None and verify_password("password123", row.password_hash)


async def test_api_token_lookup(session, user):
    tok = generate_api_token()
    session.add(models.ApiToken(user_id=user.id, label="t", token_hash=hash_api_token(tok)))
    await session.commit()
    assert (await user_from_api_token(tok, session)).id == user.id
    assert await user_from_api_token("nope", session) is None


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
