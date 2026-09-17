from datetime import UTC, datetime

import pytest

from pensieve.fetch.parse import (
    ParseError,
    content_hash,
    html_to_text,
    parse_feed,
    sanitize_html,
)
from tests.test_fetch_helpers import ATOM, HTML_NO_LINKS, JSON_FEED, RDF, RSS


def test_parse_rss():
    feed = parse_feed(RSS, "https://example.com/feed.xml")
    assert feed.kind == "rss20"
    assert feed.title == "Example & Co"
    assert feed.site_url == "https://example.com/"
    assert feed.description == "News from example"
    assert feed.icon_url == "https://example.com/logo.png"
    assert feed.hub == "https://hub.example.com/"
    assert len(feed.entries) == 2

    first = feed.entries[0]
    assert first.guid == "post-1"
    assert first.url == "https://example.com/posts/1"  # relative link resolved against site
    assert first.title == "First post"  # markup stripped from title
    assert first.author == "Alice"
    assert first.published_at == datetime(2024, 1, 1, 8, 0, tzinfo=UTC)  # +02:00 normalised to UTC
    assert first.published_at.tzinfo is not None
    assert "<script" not in first.content_html and "onclick" not in first.content_html
    assert 'href="https://example.com/x"' in first.content_html
    assert 'rel="noopener noreferrer nofollow"' in first.content_html and 'target="_blank"' in first.content_html
    assert first.content_text == "Hello link"
    assert first.summary == "Short summary"
    assert first.enclosure_url == "https://example.com/ep1.mp3" and first.enclosure_type == "audio/mpeg"
    assert len(first.hash) == 64

    second = feed.entries[1]
    assert second.guid == "https://example.com/posts/2"  # guid falls back to link
    assert second.content_html == "<p>Body two</p>"
    assert second.summary == "Body two"


def test_parse_atom():
    feed = parse_feed(ATOM, "https://atom.example.org/feed.atom")
    assert feed.kind == "atom10"
    assert feed.title == "Atom Example" and feed.description == "An atom feed"
    assert feed.site_url == "https://atom.example.org/"
    assert feed.icon_url == "https://atom.example.org/icon.png"
    assert feed.hub == "https://pubsubhubbub.example.org/"

    e1, e2 = feed.entries
    assert e1.guid == "urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a"
    assert e1.url == "https://atom.example.org/entries/1"
    assert e1.title == "Atom entry"
    assert e1.author == "Bob"  # feed-level author inherited
    assert e1.published_at == datetime(2024, 2, 1, 6, 30, tzinfo=UTC)  # updated used when no published
    assert "onerror" not in e1.content_html
    assert '<img src="https://atom.example.org/pic.png">' in e1.content_html
    assert "<h2>Heading</h2>" in e1.content_html
    assert e1.content_text == "Heading\nContent with"

    # No id and no link: sha256(title + published) fallback, stable across parses
    assert len(e2.guid) == 64 and e2.url is None
    assert e2.guid == parse_feed(ATOM, "https://atom.example.org/feed.atom").entries[1].guid
    assert e2.content_text == "Just a summary"


def test_parse_json_feed():
    feed = parse_feed(JSON_FEED, "https://json.example.net/feed.json")
    assert feed.kind == "json"
    assert feed.title == "JSON Example" and feed.site_url == "https://json.example.net/"
    assert feed.icon_url == "https://json.example.net/icon.png"
    assert feed.hub == "https://hub.json.example.net/"
    one, two = feed.entries
    assert one.guid == "1" and one.url == "https://json.example.net/1"
    assert one.author == "Carol"
    assert one.published_at == datetime(2024, 3, 1, 13, 0, tzinfo=UTC)
    assert "iframe" not in one.content_html and one.content_text == "One"
    assert one.enclosure_url == "https://json.example.net/1.mp4" and one.enclosure_type == "video/mp4"
    assert two.author == "Dave"
    assert two.content_html == "<p>Plain<br>text</p>"
    assert two.content_text == "Plain\ntext"


def test_parse_rss10_rdf():
    feed = parse_feed(RDF, "https://rdf.example.com/rss")
    assert feed.title == "RDF Example"
    assert feed.entries[0].guid == "https://rdf.example.com/a"
    assert feed.entries[0].published_at == datetime(2024, 4, 1, tzinfo=UTC)


def test_published_falls_back_to_now():
    before = datetime.now(UTC)
    feed = parse_feed(b'<rss version="2.0"><channel><title>t</title><item><title>x</title></item></channel></rss>')
    entry = feed.entries[0]
    assert entry.published_at.tzinfo is UTC
    assert entry.published_at >= before
    assert len(entry.guid) == 64  # no id, no link


def test_html_is_not_a_feed():
    with pytest.raises(ParseError):
        parse_feed(HTML_NO_LINKS, "https://example.com/")
    with pytest.raises(ParseError):
        parse_feed(b'{"not": "a feed"}')


def test_sanitizer_strips_dangerous_content():
    raw = (
        '<div onmouseover="steal()"><p style="color:red" onclick="x()">Hi</p>'
        "<script>alert(1)</script><style>p{display:none}</style>"
        '<iframe src="https://evil.example/"></iframe>'
        '<a href="javascript:alert(1)">js</a>'
        '<a href="https://ok.example/">ok</a>'
        '<img src="data:image/png;base64,AAAA"><img src="data:text/html;base64,AAAA">'
        '<img src="https://ok.example/a.png" onerror="x()">'
        "<pre><code>x &lt; y</code></pre><table><tr><td colspan=\"2\">c</td></tr></table>"
        '<video controls src="https://ok.example/v.mp4"></video></div>'
    )
    out = sanitize_html(raw)
    assert "script" not in out and "alert" not in out and "display:none" not in out
    assert "iframe" not in out and "evil.example" not in out
    assert "onclick" not in out and "onmouseover" not in out and "onerror" not in out and "style=" not in out
    assert "javascript:" not in out
    assert '<a href="https://ok.example/" rel="noopener noreferrer nofollow" target="_blank">ok</a>' in out
    assert '<img src="data:image/png;base64,AAAA">' in out
    assert "data:text/html" not in out
    assert '<img src="https://ok.example/a.png">' in out
    assert "<pre><code>x &lt; y</code></pre>" in out
    assert '<td colspan="2">c</td>' in out
    assert "<video" in out and 'src="https://ok.example/v.mp4"' in out and "controls" in out


def test_sanitizer_resolves_relative_urls():
    out = sanitize_html('<a href="../x">l</a><img src="i.png">', base_url="https://example.com/a/b/")
    assert 'href="https://example.com/a/x"' in out and 'src="https://example.com/a/b/i.png"' in out


def test_html_to_text_blocks_and_entities():
    assert html_to_text("<p>One &amp; two</p><ul><li>a</li><li>b</li></ul><br>c") == "One & two\na\nb\nc"
    assert html_to_text("") == ""


def test_hash_is_stable_and_normalised():
    h1 = content_hash("Hello World", "Body   text\nhere")
    assert h1 == content_hash("hello world", "body text here")
    assert h1 == content_hash("  HELLO   WORLD ", "Body text here  ")
    assert h1 != content_hash("Hello World", "different body")
    assert h1 != content_hash("Other", "Body text here")
    assert len(h1) == 64
    # Same article in two feeds hashes the same
    a = parse_feed(RSS, "https://example.com/feed.xml").entries[0]
    b = parse_feed(RSS.replace(b"post-1", b"other-guid"), "https://mirror.example.com/rss").entries[0]
    assert a.hash == b.hash and a.guid != b.guid
