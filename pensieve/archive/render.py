"""Render a page in headless Chromium (the compose ``browser`` service) and collect what it loaded.

The browser runs in its own container on an egress-only network; the worker drives it over the Playwright
protocol. Every request Chromium makes goes through :func:`_guard` first, which applies the same SSRF policy
as the fetcher (``fetch.http.validate_url``), so a saved page can't make the browser reach the LAN, the
database or the object store. WebSockets are refused outright and service workers are blocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from pensieve.config import get_settings
from pensieve.fetch.http import UnsafeURLError, validate_url

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36 Pensieve/0.1"
)
VIEWPORT = {"width": 1280, "height": 900}
KEEP_TYPES = {"image", "stylesheet", "font"}
MAX_RESOURCE_BYTES = 15 * 1024 * 1024


class RenderError(Exception):
    pass


@dataclass
class Resource:
    url: str
    content_type: str
    body: bytes

    @property
    def sha256(self) -> str:
        cached = self.__dict__.get("_sha")
        if cached is None:
            cached = self.__dict__["_sha"] = hashlib.sha256(self.body).hexdigest()
        return cached


@dataclass
class Rendered:
    url: str
    """Final URL after redirects."""
    status: int | None
    html: str
    """The DOM after scripts ran, with lazy images resolved to what actually loaded."""
    title: str = ""
    resources: dict[str, Resource] = field(default_factory=dict)
    screenshot: bytes | None = None
    icon_url: str | None = None
    blocked: int = 0
    """Requests refused by the SSRF guard (reported on the snapshot when the page came out empty)."""


class Renderer(Protocol):
    async def render(self, url: str) -> Rendered: ...


# Scroll the page in steps so lazy loaders fire, then come back to the top.
AUTOSCROLL_JS = """
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let last = -1;
  for (let i = 0; i < 30; i++) {
    window.scrollBy(0, Math.round(window.innerHeight * 0.9));
    await sleep(180);
    const y = window.scrollY;
    if (y === last) break;
    last = y;
  }
  window.scrollTo(0, 0);
  await sleep(150);
}
"""

# Pin every image to the file the browser actually chose, so the frozen copy needs exactly what loaded.
PREPARE_JS = """
() => {
  for (const img of document.querySelectorAll('img')) {
    const chosen = img.currentSrc || img.src;
    const lazy = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('data-original');
    if (chosen && !chosen.startsWith('data:image/gif')) img.setAttribute('src', chosen);
    else if (lazy) img.setAttribute('src', new URL(lazy, document.baseURI).href);
    img.removeAttribute('srcset'); img.removeAttribute('sizes'); img.removeAttribute('loading');
  }
  for (const s of document.querySelectorAll('picture source')) s.remove();
  for (const el of document.querySelectorAll('input, textarea')) {
    if (el.type === 'password' || el.type === 'hidden') continue;
    if (el.tagName === 'TEXTAREA') el.textContent = el.value; else el.setAttribute('value', el.value);
  }
  for (const d of document.querySelectorAll('details[open]')) d.setAttribute('open', '');
  const icon = document.querySelector('link[rel~="icon"], link[rel="apple-touch-icon"]');
  return {
    icon: icon ? icon.href : new URL('/favicon.ico', location.href).href,
    height: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),
  };
}
"""


class PlaywrightRenderer:
    def __init__(self, ws_endpoint: str | None = None) -> None:
        s = get_settings()
        self.ws_endpoint = ws_endpoint or s.browser_ws
        self.timeout_s = s.capture_timeout_s
        self.max_asset_bytes = s.capture_max_asset_mb * 1024 * 1024
        self.shot_max_px = s.capture_shot_max_px

    async def render(self, url: str) -> Rendered:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # the capture image installs the `capture` extra
            raise RenderError("playwright is not installed in this image") from exc
        async with asyncio.timeout(self.timeout_s + 45), async_playwright() as p:
            try:
                browser = await p.chromium.connect(self.ws_endpoint, timeout=15_000)
            except Exception as exc:
                raise RenderError(f"browser unavailable at {self.ws_endpoint}: {exc}") from exc
            try:
                return await self._render(browser, url)
            finally:
                await browser.close()

    async def _render(self, browser, url: str) -> Rendered:
        context = await browser.new_context(
            viewport=VIEWPORT,
            user_agent=BROWSER_UA,
            locale="en-US",
            service_workers="block",
            accept_downloads=False,
            java_script_enabled=True,
        )
        verdicts: dict[str, bool] = {}
        blocked = 0

        async def _guard(route) -> None:
            nonlocal blocked
            request = route.request
            parts = urlsplit(request.url)
            if parts.scheme in {"data", "blob", "about"}:
                await route.continue_()
                return
            host = (parts.hostname or "").lower()
            ok = verdicts.get(host)
            if ok is None:
                try:
                    await asyncio.to_thread(validate_url, request.url)
                    ok = True
                except UnsafeURLError:
                    ok = False
                verdicts[host] = ok
            if not ok or request.resource_type == "media":
                blocked += 0 if ok else 1
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", _guard)
        if hasattr(context, "route_web_socket"):
            # A handler that never calls connect_to_server() keeps the socket from reaching any server.
            await context.route_web_socket("**", lambda ws: None)

        resources: dict[str, Resource] = {}
        kept = 0
        pending: list[asyncio.Task] = []

        async def _keep(response) -> None:
            nonlocal kept
            try:
                if response.request.resource_type not in KEEP_TYPES or response.status != 200:
                    return
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_RESOURCE_BYTES:
                    return
                body = await response.body()
                if len(body) > MAX_RESOURCE_BYTES or kept + len(body) > self.max_asset_bytes:
                    return
                kept += len(body)
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                resources[response.url] = Resource(response.url, ctype or "application/octet-stream", body)
            except Exception:  # noqa: BLE001 - a body that went away is just not archived
                return

        page = await context.new_page()
        page.on("response", lambda r: pending.append(asyncio.ensure_future(_keep(r))))
        timeout_ms = int(self.timeout_s * 1000)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            raise RenderError(f"navigation failed: {exc}") from exc
        status = response.status if response else None
        for state, wait in (("load", 15_000), ("networkidle", 8_000)):
            try:
                await page.wait_for_load_state(state, timeout=wait)
            except Exception as exc:  # noqa: BLE001 - busy pages never go idle; carry on with what rendered
                log.debug("%s: no %s state: %s", url, state, exc.__class__.__name__)
        try:
            await page.evaluate(AUTOSCROLL_JS)
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s: autoscroll: %s", url, exc.__class__.__name__)
        info = await page.evaluate(PREPARE_JS)
        shot = None
        try:
            height = max(VIEWPORT["height"], min(int(info.get("height") or 0), self.shot_max_px))
            shot = await page.screenshot(
                full_page=True,
                type="jpeg",
                quality=72,
                clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height},
                animations="disabled",
                timeout=25_000,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("screenshot of %s failed: %s", url, exc)
        html = await page.content()
        title = await page.title()
        final_url = page.url
        if pending:
            await asyncio.wait(pending, timeout=10)
        await context.close()
        return Rendered(
            url=final_url,
            status=status,
            html=html,
            title=title or "",
            resources=resources,
            screenshot=shot,
            icon_url=info.get("icon"),
            blocked=blocked,
        )


_renderer: Renderer | None = None


def set_renderer(renderer: Renderer | None) -> None:
    """Tests install a fake; ``None`` restores the Playwright renderer."""
    global _renderer
    _renderer = renderer


def get_renderer() -> Renderer | None:
    if _renderer is not None:
        return _renderer
    return PlaywrightRenderer() if get_settings().browser_ws else None


__all__ = [
    "BROWSER_UA",
    "PlaywrightRenderer",
    "RenderError",
    "Rendered",
    "Renderer",
    "Resource",
    "get_renderer",
    "set_renderer",
]
