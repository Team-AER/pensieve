"""Keyset cursor over ``(published_at, id)`` for the reader list, mirroring the sync API's continuation
tokens (``pensieve.syncapi.common``) without depending on that package."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pensieve.models import Item


def to_usec(dt: datetime) -> int:
    return int(dt.timestamp()) * 1_000_000 + dt.microsecond


def from_usec(usec: int) -> datetime:
    return datetime.fromtimestamp(0, UTC) + timedelta(microseconds=usec)


def encode_cursor(item: Item) -> str:
    return f"{to_usec(item.published_at)}.{item.id.hex}"


def decode_cursor(raw: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not raw:
        return None
    try:
        usec, hexid = raw.split(".", 1)
        return from_usec(int(usec)), uuid.UUID(hex=hexid)
    except ValueError:
        return None


__all__ = ["decode_cursor", "encode_cursor", "from_usec", "to_usec"]
