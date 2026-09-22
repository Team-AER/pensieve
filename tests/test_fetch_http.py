import asyncio

import httpx
import pytest
import respx

from pensieve.config import get_settings
from pensieve.fetch import http as fetch_http
from pensieve.fetch.http import UnsafeURLError, assert_safe_url
from tests.test_fetch_helpers import no_dns  # noqa: F401


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed",
        "http://127.1.2.3:8080/",
        "http://10.0.0.5/rss",
        "http://172.16.4.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/",
        "http://localhost:8000/",
        "http://foo.localhost/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "ftp://example.com/feed",
        "file:///etc/passwd",
        "gopher://example.com/",
        "https://user:pw@example.com/",
        "not a url",
    ],
)
def test_ssrf_guard_rejects(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)


def test_ssrf_guard_rejects_hostname_resolving_to_private(monkeypatch):
    monkeypatch.setattr(fetch_http, "resolve_host", lambda host: ["93.184.216.34", "192.168.0.10"])
    with pytest.raises(UnsafeURLError):
        assert_safe_url("https://evil.example.com/")


def test_ssrf_guard_allows_public():
    assert_safe_url("https://example.com/feed.xml")
    assert_safe_url("http://93.184.216.34/")


def test_ssrf_guard_relaxed_in_debug(monkeypatch):
    monkeypatch.setattr(get_settings(), "debug", True)
    assert_safe_url("http://127.0.0.1:8000/feed")
    with pytest.raises(UnsafeURLError):
        assert_safe_url("ftp://127.0.0.1/")  # scheme check always applies


async def test_client_sends_user_agent_and_follows_redirects():
    settings = get_settings()
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.com/old").respond(301, headers={"Location": "https://example.com/new"})
        route = router.get("https://example.com/new").respond(200, text="ok")
        response = await fetch_http.get("https://example.com/old")
    assert response.status_code == 200 and str(response.url) == "https://example.com/new"
    request = route.calls.last.request
    assert request.headers["user-agent"] == settings.fetch_user_agent
    assert "gzip" in request.headers["accept-encoding"]


async def test_redirect_to_private_address_is_blocked():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://example.com/r").respond(302, headers={"Location": "http://127.0.0.1:9/admin"})
        internal = router.get("http://127.0.0.1:9/admin").respond(200, text="secret")
        with pytest.raises(UnsafeURLError):
            await fetch_http.get("https://example.com/r")
    assert not internal.called


async def test_get_refuses_unsafe_before_connecting():
    with respx.mock(assert_all_called=False) as router:
        internal = router.get("http://10.0.0.1/").respond(200)
        with pytest.raises(UnsafeURLError):
            await fetch_http.get("http://10.0.0.1/")
    assert not internal.called


async def test_per_host_concurrency_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "fetch_per_host_concurrency", 1)
    fetch_http._host_semaphores.clear()
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, text="x")

    with respx.mock() as router:
        router.get(host="slow.example.com").mock(side_effect=handler)
        async with fetch_http.get_client() as client:
            await asyncio.gather(
                *(fetch_http.get(f"https://slow.example.com/{i}", client=client) for i in range(4))
            )
    assert peak == 1

    # Different hosts do not share a limiter.
    fetch_http._host_semaphores.clear()
    peak = 0
    with respx.mock() as router:
        router.get(host__regex=r"h\d\.example\.com").mock(side_effect=handler)
        async with fetch_http.get_client() as client:
            await asyncio.gather(
                *(fetch_http.get(f"https://h{i}.example.com/", client=client) for i in range(3))
            )
    assert peak == 3
