"""Shared fixtures and inline feed documents for the fetch package tests (no tests of its own)."""

from __future__ import annotations

import pytest

from pensieve.fetch import http as fetch_http

PUBLIC_IP = "93.184.216.34"

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Example &amp; Co</title>
  <link>https://example.com/</link>
  <description>News from example</description>
  <image><url>https://example.com/logo.png</url><title>x</title><link>https://example.com/</link></image>
  <atom:link rel="hub" href="https://hub.example.com/"/>
  <item>
    <title>First &lt;b&gt;post&lt;/b&gt;</title>
    <link>/posts/1</link>
    <guid isPermaLink="false">post-1</guid>
    <author>alice@example.com (Alice)</author>
    <pubDate>Mon, 01 Jan 2024 10:00:00 +0200</pubDate>
    <description>Short summary</description>
    <content:encoded><![CDATA[<p>Hello <script>alert(1)</script><a href="/x" onclick="evil()">link</a></p>]]></content:encoded>
    <enclosure url="https://example.com/ep1.mp3" type="audio/mpeg" length="1"/>
  </item>
  <item>
    <title>Second post</title>
    <link>https://example.com/posts/2</link>
    <pubDate>Tue, 02 Jan 2024 10:00:00 GMT</pubDate>
    <description><![CDATA[<p>Body two</p>]]></description>
  </item>
</channel>
</rss>
"""

RSS_MORE = RSS.replace(
    b"  <item>\n    <title>Second post</title>",
    b"""  <item>
    <title>Third post</title>
    <link>https://example.com/posts/3</link>
    <pubDate>Wed, 03 Jan 2024 10:00:00 GMT</pubDate>
    <description>Body three</description>
  </item>
  <item>
    <title>Second post</title>""",
)

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <subtitle>An atom feed</subtitle>
  <link href="https://atom.example.org/"/>
  <link rel="self" href="https://atom.example.org/feed.atom"/>
  <link rel="hub" href="https://pubsubhubbub.example.org/"/>
  <icon>/icon.png</icon>
  <updated>2024-02-01T12:00:00Z</updated>
  <author><name>Bob</name></author>
  <entry>
    <id>urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a</id>
    <title type="html">Atom &lt;em&gt;entry&lt;/em&gt;</title>
    <link rel="alternate" href="/entries/1"/>
    <updated>2024-02-01T12:00:00+05:30</updated>
    <content type="html"><![CDATA[<div><h2>Heading</h2><p>Content with <img src="/pic.png" onerror="x()"></p></div>]]></content>
  </entry>
  <entry>
    <title>No id entry</title>
    <updated>2024-02-02T00:00:00Z</updated>
    <summary>Just a summary</summary>
  </entry>
</feed>
"""

JSON_FEED = b"""{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "JSON Example",
  "home_page_url": "https://json.example.net/",
  "feed_url": "https://json.example.net/feed.json",
  "description": "A json feed",
  "icon": "https://json.example.net/icon.png",
  "hubs": [{"type": "WebSub", "url": "https://hub.json.example.net/"}],
  "authors": [{"name": "Carol"}],
  "items": [
    {
      "id": "1",
      "url": "https://json.example.net/1",
      "title": "JSON one",
      "content_html": "<p>One <iframe src=\\"https://evil\\"></iframe></p>",
      "date_published": "2024-03-01T08:00:00-05:00",
      "attachments": [{"url": "https://json.example.net/1.mp4", "mime_type": "video/mp4"}]
    },
    {
      "id": "2",
      "url": "https://json.example.net/2",
      "title": "JSON two",
      "content_text": "Plain\\ntext",
      "authors": [{"name": "Dave"}]
    }
  ]
}
"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://rdf.example.com/"><title>RDF Example</title><link>https://rdf.example.com/</link>
    <description>rss 1.0</description></channel>
  <item rdf:about="https://rdf.example.com/a"><title>RDF item</title><link>https://rdf.example.com/a</link>
    <dc:date>2024-04-01T00:00:00Z</dc:date><description>rdf body</description></item>
</rdf:RDF>
"""

HTML_WITH_LINKS = b"""<!doctype html><html><head><title>Site</title>
<link rel="alternate" type="application/rss+xml" title="RSS" href="/feeds/all.rss">
<link rel="alternate" type="application/feed+json" href="/feed.json">
<link rel="hub" href="https://hub.example.com/">
<link rel="shortcut icon" href="/static/fav.png">
</head><body><h1>Hi</h1></body></html>"""

HTML_NO_LINKS = b"<!doctype html><html><head><title>Bare</title></head><body>nothing</body></html>"


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Never resolve real hostnames in tests: everything is a public address unless a test overrides it."""
    monkeypatch.setattr(fetch_http, "resolve_host", lambda host: [PUBLIC_IP])


@pytest.fixture
def fake_queue(monkeypatch):
    """Capture ``pensieve.queue.enqueue`` calls so Redis is never touched."""
    calls: list[tuple] = []

    async def enqueue(function, *args, _job_id=None, **kwargs):
        calls.append((function, args, _job_id))

    monkeypatch.setattr("pensieve.queue.enqueue", enqueue)
    return calls
