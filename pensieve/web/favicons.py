"""Serve cached feed favicons (``feeds.icon_data``) with long cache headers; letter avatar fallback."""

from __future__ import annotations

import hashlib
import html
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from pensieve.models import Feed
from pensieve.web.templating import DB, CurrentUser

router = APIRouter()

CACHE_CONTROL = "public, max-age=604800, stale-while-revalidate=86400"
PALETTE = ("#C9532C", "#2C7A7B", "#5A67D8", "#B7791F", "#2F855A", "#97266D", "#4A5568", "#D69E2E")


def letter_avatar_svg(title: str, seed: str) -> str:
    """A 32x32 SVG with the feed's initial on a colour picked from its id."""
    letter = next((c for c in (title or "").strip() if c.isalnum()), "#").upper()
    colour = PALETTE[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(PALETTE)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="7" fill="{colour}"/>'
        '<text x="16" y="21.5" text-anchor="middle" font-family="system-ui, sans-serif" font-size="17" '
        f'font-weight="600" fill="#fff">{html.escape(letter)}</text></svg>'
    )


@router.get("/favicons/{feed_id}")
async def favicon(request: Request, feed_id: uuid.UUID, user: CurrentUser, session: DB) -> Response:
    row = (
        await session.execute(
            select(Feed.title, Feed.url, Feed.icon_data, Feed.icon_content_type).where(
                Feed.id == feed_id, Feed.user_id == user.id
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    title, url, data, content_type = row
    if data:
        body, media_type = bytes(data), content_type or "image/x-icon"
    else:
        body, media_type = letter_avatar_svg(title or url, str(feed_id)).encode(), "image/svg+xml"
    etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)


__all__ = ["letter_avatar_svg", "router"]
