"""Outbound HTTP for the fetcher: client factory, SSRF guard and per-host concurrency limits.

Every outbound request made through :func:`get_client` passes through :func:`assert_safe_url` (also on
redirect hops, via an httpx request hook) so no caller can accidentally reach loopback, link-local, RFC1918 or
cloud metadata addresses. ``settings.debug`` relaxes the address checks for local development; the scheme
check always applies.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from pensieve.config import get_settings

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames that are always refused, even before DNS. Cloud metadata endpoints and local names.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
BLOCKED_HOST_SUFFIXES = (".localhost", ".internal", ".local")


class UnsafeURLError(ValueError):
    """Raised when a URL must not be fetched (scheme or destination address)."""


def resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to a list of IP literals. Module-level so tests can monkeypatch it (no DNS)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host {host!r}: {exc}") from exc
    return sorted({info[4][0] for info in infos})


def _address_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_reserved:
        return False
    if ip.is_unspecified:
        return False
    # ``is_global`` is False for shared address space (100.64/10), benchmarking and documentation ranges too.
    return bool(ip.is_global)


def _check_addresses(host: str, addresses: Iterable[str]) -> None:
    addresses = list(addresses)
    if not addresses:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeURLError(f"host {host!r} resolved to invalid address {raw!r}") from exc
        if not _address_is_public(ip):
            raise UnsafeURLError(f"host {host!r} resolves to non-public address {raw}")


def assert_safe_url(url: str) -> None:
    """Refuse non-http(s) URLs and, unless ``settings.debug``, any URL pointing at a non-public address.

    Hostnames are resolved with the system resolver so DNS tricks (``127.0.0.1.nip.io``) are caught.
    Raises :class:`UnsafeURLError`.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError(f"malformed URL {url!r}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parts.scheme!r} is not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    if get_settings().debug:
        return

    lowered = host.lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError(f"host {host!r} is blocked")

    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        _check_addresses(host, [str(literal)])
        return
    _check_addresses(host, resolve_host(lowered))


async def ensure_safe_url(url: str) -> None:
    """Async variant of :func:`assert_safe_url` (DNS resolution runs in a worker thread)."""
    await asyncio.to_thread(assert_safe_url, url)


async def _guard_request(request: httpx.Request) -> None:
    await ensure_safe_url(str(request.url))


def get_client(**overrides) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` configured from settings; every request (incl. redirects) is SSRF-guarded."""
    settings = get_settings()
    kwargs: dict = {
        "headers": {
            "User-Agent": settings.fetch_user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/feed+json, application/json, "
            "application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        },
        "timeout": httpx.Timeout(settings.fetch_timeout_s),
        "follow_redirects": True,
        "max_redirects": 5,
        "event_hooks": {"request": [_guard_request]},
    }
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)


# --------------------------------------------------------------------------------------------------------------
# Per-host concurrency
# --------------------------------------------------------------------------------------------------------------

_host_semaphores: dict[str, asyncio.Semaphore] = {}
_semaphore_limit: int | None = None


def _semaphore_for(host: str) -> asyncio.Semaphore:
    global _semaphore_limit
    limit = max(1, get_settings().fetch_per_host_concurrency)
    if _semaphore_limit != limit:
        _host_semaphores.clear()
        _semaphore_limit = limit
    sem = _host_semaphores.get(host)
    if sem is None:
        sem = _host_semaphores[host] = asyncio.Semaphore(limit)
    return sem


@asynccontextmanager
async def host_slot(url: str) -> AsyncIterator[None]:
    """Limit concurrent requests per host to ``settings.fetch_per_host_concurrency``."""
    host = (urlsplit(url).hostname or "").lower()
    async with _semaphore_for(host):
        yield


async def get(url: str, *, client: httpx.AsyncClient | None = None, headers: dict | None = None) -> httpx.Response:
    """GET ``url`` with the SSRF guard and per-host limiter; opens a temporary client when none is passed."""
    await ensure_safe_url(url)
    async with host_slot(url):
        if client is not None:
            return await client.get(url, headers=headers)
        async with get_client() as own:
            return await own.get(url, headers=headers)


__all__ = [
    "UnsafeURLError",
    "assert_safe_url",
    "ensure_safe_url",
    "get",
    "get_client",
    "host_slot",
    "resolve_host",
]
