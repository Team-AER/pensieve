"""Fixtures for the archive tests: an in-memory bucket, a scripted browser, a captured queue."""

from __future__ import annotations

import pytest

from pensieve.archive import render as render_mod
from pensieve.archive import storage as storage_mod
from pensieve.archive.render import Rendered, RenderError, Resource

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"

ARTICLE_TEXT = (
    "The orchard survey counted every apple tree in the valley. Growers reported the heaviest harvest in a "
    "decade, and the cooperative plans a second press. Volunteers mapped rows, measured trunk girth and noted "
    "which saplings survived the late frost. The report closes with a plan to graft heritage varieties. "
)

RENDERED_DOM = f"""<!doctype html><html lang="en"><head><title>Orchard survey</title>
<meta property="og:site_name" content="Valley Gazette"><meta property="og:image" content="https://news.example.com/lead.png">
<link rel="stylesheet" href="https://news.example.com/site.css"><link rel="preload" href="/x.js" as="script">
<script>window.track = 1</script></head>
<body onload="evil()"><nav>Home Sections Subscribe</nav>
<article><h1>Orchard survey finds a record harvest</h1>
<p>{ARTICLE_TEXT}</p><p><img src="https://news.example.com/pic.png" alt="trees"></p>
<p>{ARTICLE_TEXT}</p><p>Second part: <a href="/more" onclick="x()">more</a> and <a href="javascript:alert(1)">bad</a>.</p>
</article><iframe src="https://video.example.com/embed/1"></iframe><footer>(c) Gazette</footer></body></html>"""

# What the server sends before scripts run: an app shell with no article.
SPA_SHELL = """<!doctype html><html><head><title>Loading…</title></head><body><div id="root"></div>
<script src="/app.js"></script></body></html>"""


def rendered(url: str = "https://news.example.com/story", dom: str = RENDERED_DOM, **kw) -> Rendered:
    resources = {
        "https://news.example.com/site.css": Resource(
            "https://news.example.com/site.css", "text/css", b"body{background:url(/bg.png)} h1{color:red}"
        ),
        "https://news.example.com/bg.png": Resource(
            "https://news.example.com/bg.png", "image/png", PNG + b"bg"
        ),
        "https://news.example.com/pic.png": Resource("https://news.example.com/pic.png", "image/png", PNG),
        "https://news.example.com/lead.png": Resource(
            "https://news.example.com/lead.png", "image/png", PNG + b"lead"
        ),
    }
    return Rendered(
        url=url, status=200, html=dom, title="Orchard survey", resources=resources, screenshot=JPEG, **kw
    )


class FakeRenderer:
    def __init__(self, result: Rendered | Exception | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    async def render(self, url: str) -> Rendered:
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result or rendered(url)


@pytest.fixture
def bucket():
    store = storage_mod.MemoryStorage()
    storage_mod.set_storage(store)
    yield store
    storage_mod.reset_storage()


@pytest.fixture
def no_bucket():
    storage_mod.set_storage(None)
    yield
    storage_mod.reset_storage()


@pytest.fixture
def browser():
    fake = FakeRenderer()
    render_mod.set_renderer(fake)
    yield fake
    render_mod.set_renderer(None)


__all__ = ["JPEG", "PNG", "RENDERED_DOM", "SPA_SHELL", "FakeRenderer", "RenderError", "rendered"]
