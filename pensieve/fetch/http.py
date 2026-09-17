"""Outbound HTTP for the fetcher: client factory, SSRF guard and per-host concurrency limits.

Every outbound request made through :func:`get_client` passes through :func:`assert_safe_url` (also on
redirect hops, via an httpx request hook) so no caller can accidentally reach loopback, link-local, RFC1918 or
cloud metadata addresses. ``settings.debug`` relaxes the address checks for local development; the scheme
check always applies.

DNS rebinding is closed by :class:`PinnedBackend`: the hook resolves and validates a hostname once and records
the addresses; the connection is then opened to one of *those* addresses (never a fresh lookup) while TLS still
uses the hostname for SNI and certificate verification. Bodies are streamed and abandoned past
``settings.fetch_max_bytes``; every :func:`get` is bounded by ``settings.fetch_total_timeout_s``.
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpcore
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


class ResponseTooLarge(httpx.HTTPError):
    """The body exceeded ``settings.fetch_max_bytes`` (decoded) and was abandoned."""


#: Addresses validated by the request hook for the current task, keyed by lower-cased hostname. The pinned
#: network backend connects to these instead of resolving again, so the address checked is the address used.
_validated_addresses: contextvars.ContextVar[dict[str, list[str]] | None] = contextvars.ContextVar(
    "pensieve_validated_addresses", default=None
)


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


def _check_addresses(host: str, addresses: Iterable[str]) -> list[str]:
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
    return addresses


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def validate_url(url: str) -> tuple[str, list[str]]:
    """Like :func:`assert_safe_url` but returns ``(hostname, validated addresses)``.

    Addresses are empty for IP literals and in ``settings.debug`` (nothing to pin).
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

    lowered = host.lower().rstrip(".")
    if get_settings().debug:
        return lowered, []

    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError(f"host {host!r} is blocked")

    if _is_ip_literal(lowered):
        _check_addresses(host, [lowered])
        return lowered, []
    return lowered, _check_addresses(host, resolve_host(lowered))


def assert_safe_url(url: str) -> None:
    """Refuse non-http(s) URLs and, unless ``settings.debug``, any URL pointing at a non-public address.

    Hostnames are resolved with the system resolver so DNS tricks (``127.0.0.1.nip.io``) are caught.
    Raises :class:`UnsafeURLError`.
    """
    validate_url(url)


async def ensure_safe_url(url: str) -> None:
    """Async variant of :func:`assert_safe_url` (DNS resolution runs in a worker thread)."""
    await asyncio.to_thread(assert_safe_url, url)


async def _guard_request(request: httpx.Request) -> None:
    """Request hook: validate the destination and pin the resolved addresses for :class:`PinnedBackend`."""
    host, addresses = await asyncio.to_thread(validate_url, str(request.url))
    if addresses:
        pinned = _validated_addresses.get()
        if pinned is None:
            pinned = {}
            _validated_addresses.set(pinned)
        pinned[host] = addresses


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects to the address the SSRF guard validated.

    ``connect_tcp`` receives the URL's hostname; instead of letting the OS resolve it again (the classic
    DNS-rebinding window) it looks up the addresses the request hook validated in this task, or resolves and
    validates itself when none were recorded (e.g. a client used without the hook). TLS is unaffected:
    httpcore wraps the socket with ``server_hostname=<hostname>`` so SNI and certificate checks use the name.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    def _addresses_for(self, host: str) -> list[str]:
        lowered = host.lower().rstrip(".")
        if get_settings().debug or _is_ip_literal(lowered):
            return [lowered]
        pinned = _validated_addresses.get() or {}
        addresses = pinned.get(lowered)
        if addresses:
            return addresses
        if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_HOST_SUFFIXES):
            raise UnsafeURLError(f"host {host!r} is blocked")
        return _check_addresses(host, resolve_host(lowered))

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await asyncio.to_thread(self._addresses_for, host)
        except UnsafeURLError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, OSError) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose connection pool uses :class:`PinnedBackend`."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        pool = getattr(self, "_pool", None)
        if isinstance(pool, httpcore.AsyncConnectionPool):
            pool._network_backend = PinnedBackend(pool._network_backend)


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
        "transport": PinnedTransport(),
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


async def _read_capped(response: httpx.Response, max_bytes: int) -> httpx.Response:
    """Stream the body up to ``max_bytes`` (decoded) and return a fully-read response; abandon it otherwise."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        await response.aclose()
        raise ResponseTooLarge(f"response declares {declared} bytes (limit {max_bytes})")
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(f"response exceeded {max_bytes} bytes")
            chunks.append(chunk)
    finally:
        await response.aclose()
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=response.request,
        history=list(response.history),
        extensions=response.extensions,
        default_encoding=response.default_encoding,
    )


async def _get_streamed(client: httpx.AsyncClient, url: str, headers: dict | None, max_bytes: int) -> httpx.Response:
    request = client.build_request("GET", url, headers=headers)
    response = await client.send(request, stream=True)
    return await _read_capped(response, max_bytes)


async def get(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    headers: dict | None = None,
    max_bytes: int | None = None,
) -> httpx.Response:
    """GET ``url`` with the SSRF guard, per-host limiter, body cap and total timeout.

    Opens a temporary client when none is passed. Raises :class:`ResponseTooLarge` (an ``httpx.HTTPError``)
    past ``max_bytes`` and ``TimeoutError`` past ``settings.fetch_total_timeout_s``.
    """
    settings = get_settings()
    limit = max_bytes if max_bytes is not None else settings.fetch_max_bytes
    await ensure_safe_url(url)
    async with asyncio.timeout(settings.fetch_total_timeout_s), host_slot(url):
        if client is not None:
            return await _get_streamed(client, url, headers, limit)
        async with get_client() as own:
            return await _get_streamed(own, url, headers, limit)


__all__ = [
    "PinnedBackend",
    "PinnedTransport",
    "ResponseTooLarge",
    "UnsafeURLError",
    "assert_safe_url",
    "ensure_safe_url",
    "get",
    "get_client",
    "host_slot",
    "resolve_host",
    "validate_url",
]
