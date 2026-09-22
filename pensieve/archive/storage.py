"""Blob storage for the page archive: any S3 API (Garage in compose; RustFS or AWS work unchanged).

Only this module talks to the bucket. Keys:
  ``snap/<snapshot id>/<generation>/{raw.html.gz,page.html.gz,shot.jpg,file}``  one capture
  ``assets/<sha[:2]>/<sha256>``                                                  shared images, fonts, icons
With no access key configured :func:`get_storage` returns ``None`` and capture keeps text only (in Postgres).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from pensieve.config import get_settings

log = logging.getLogger(__name__)


class Storage(Protocol):
    async def put(
        self, key: str, data: bytes, content_type: str, content_encoding: str | None = None
    ) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, keys: list[str]) -> None: ...

    def list_prefix(self, prefix: str) -> AsyncIterator[str]: ...


def asset_key(sha256: str) -> str:
    return f"assets/{sha256[:2]}/{sha256}"


def snapshot_prefix(snapshot_id: object, generation: int | None = None) -> str:
    base = f"snap/{snapshot_id}/"
    return base if generation is None else f"{base}{generation}/"


class MemoryStorage:
    """In-process store for tests and for installs that want to try the feature without a bucket."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str | None]] = {}

    async def put(
        self, key: str, data: bytes, content_type: str, content_encoding: str | None = None
    ) -> None:
        self.objects[key] = (bytes(data), content_type, content_encoding)

    async def get(self, key: str) -> bytes | None:
        entry = self.objects.get(key)
        return entry[0] if entry else None

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def delete(self, keys: list[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield key


class S3Storage:
    """aiobotocore client per call: cheap next to a capture, and safe across event loops (tests, CLI, arq)."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self.region = region

    @asynccontextmanager
    async def _client(self):
        from aiobotocore.config import AioConfig
        from aiobotocore.session import get_session

        config = AioConfig(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            # Garage does not implement every flexible-checksum header newer botocore sends by default.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        async with get_session().create_client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=config,
        ) as client:
            yield client

    async def put(
        self, key: str, data: bytes, content_type: str, content_encoding: str | None = None
    ) -> None:
        extra = {"ContentEncoding": content_encoding} if content_encoding else {}
        async with self._client() as client:
            await client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra)

    async def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                    return None
                raise
            async with response["Body"] as body:
                return await body.read()

    async def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        async with self._client() as client:
            try:
                await client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                    return False
                raise
            return True

    async def delete(self, keys: list[str]) -> None:
        if not keys:
            return
        async with self._client() as client:
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                await client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
                )

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        async with self._client() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for entry in page.get("Contents", []) or []:
                    yield entry["Key"]


_override: Storage | None = None
_override_set = False


def set_storage(storage: Storage | None) -> None:
    """Tests: force a store (``MemoryStorage()``), or ``None`` for the text-only mode. Reset with reset_storage()."""
    global _override, _override_set
    _override, _override_set = storage, True


def reset_storage() -> None:
    global _override, _override_set
    _override, _override_set = None, False


def get_storage() -> Storage | None:
    if _override_set:
        return _override
    settings = get_settings()
    if not (settings.s3_access_key and settings.s3_secret_key and settings.s3_endpoint):
        return None
    return S3Storage(
        settings.s3_endpoint,
        settings.s3_bucket,
        settings.s3_access_key,
        settings.s3_secret_key,
        settings.s3_region,
    )


__all__ = [
    "MemoryStorage",
    "S3Storage",
    "Storage",
    "asset_key",
    "get_storage",
    "reset_storage",
    "set_storage",
    "snapshot_prefix",
]
