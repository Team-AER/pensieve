"""Idempotent bootstrap of a single-node Garage through its admin API (v2).

Run by the compose one-shot ``storage-init`` before the app starts: assign the node a layout role, import the
access key from ``.env`` (so the app's credentials never need to be read back), create the bucket and grant the
key read/write on it. Every step checks first, so re-running on each ``compose up`` is a no-op.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from pensieve.config import get_settings

log = logging.getLogger(__name__)

ZONE = "dc1"


class GarageError(RuntimeError):
    pass


async def _wait_ready(client: httpx.AsyncClient, attempts: int = 60) -> dict:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            r = await client.get("/v2/GetClusterStatus")
            if r.status_code == 200:
                return r.json()
            last = GarageError(f"GetClusterStatus: HTTP {r.status_code} {r.text[:200]}")
        except httpx.HTTPError as exc:
            last = exc
        await asyncio.sleep(1)
    raise GarageError(f"Garage admin API not reachable: {last}")


async def _call(client: httpx.AsyncClient, method: str, path: str, **kw) -> httpx.Response:
    r = await client.request(method, path, **kw)
    if r.status_code >= 400 and r.status_code != 404:
        raise GarageError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
    return r


async def init_storage() -> str:
    """Make the bucket usable with the configured key. Returns a one-line summary."""
    s = get_settings()
    if not (s.garage_admin_token and s.s3_access_key and s.s3_secret_key):
        raise GarageError(
            "set PENSIEVE_GARAGE_ADMIN_TOKEN, PENSIEVE_S3_ACCESS_KEY and PENSIEVE_S3_SECRET_KEY"
        )
    headers = {"Authorization": f"Bearer {s.garage_admin_token}"}
    done: list[str] = []
    async with httpx.AsyncClient(base_url=s.garage_admin_url, headers=headers, timeout=15) as client:
        status = await _wait_ready(client)
        layout = (await _call(client, "GET", "/v2/GetClusterLayout")).json()
        if not layout.get("roles"):
            node_id = status["nodes"][0]["id"]
            role = {
                "id": node_id,
                "zone": ZONE,
                "capacity": s.garage_capacity_gb * 1_000_000_000,
                "tags": ["pensieve"],
            }
            await _call(client, "POST", "/v2/UpdateClusterLayout", json={"roles": [role]})
            await _call(
                client, "POST", "/v2/ApplyClusterLayout", json={"version": int(layout.get("version", 0)) + 1}
            )
            done.append(f"layout applied ({s.garage_capacity_gb} GB)")

        key = await _call(client, "GET", "/v2/GetKeyInfo", params={"id": s.s3_access_key})
        if key.status_code == 404 or (key.status_code == 400 and "not found" in key.text.lower()):
            await _call(
                client,
                "POST",
                "/v2/ImportKey",
                json={"accessKeyId": s.s3_access_key, "secretAccessKey": s.s3_secret_key, "name": "pensieve"},
            )
            done.append("key imported")

        bucket = await _call(client, "GET", "/v2/GetBucketInfo", params={"globalAlias": s.s3_bucket})
        if bucket.status_code == 404:
            bucket = await _call(client, "POST", "/v2/CreateBucket", json={"globalAlias": s.s3_bucket})
            done.append(f"bucket {s.s3_bucket} created")
        bucket_id = bucket.json()["id"]
        await _call(
            client,
            "POST",
            "/v2/AllowBucketKey",
            json={
                "bucketId": bucket_id,
                "accessKeyId": s.s3_access_key,
                "permissions": {"read": True, "write": True, "owner": True},
            },
        )
    return "garage ready: " + (", ".join(done) if done else "nothing to change")


__all__ = ["GarageError", "init_storage"]
