from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.fetch.opml import OPMLError, export_opml, import_opml, parse_opml

OPML = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Subscriptions</title></head>
  <body>
    <outline text="Top feed" title="Top Feed Title" type="rss" xmlUrl="https://top.example.com/rss" htmlUrl="https://top.example.com/"/>
    <outline text="Tech">
      <outline text="Ars" type="rss" xmlUrl="https://ars.example.com/feed" htmlUrl="https://ars.example.com/"/>
      <outline text="Nested folder">
        <outline text="Deep" xmlurl="https://deep.example.com/atom.xml"/>
      </outline>
      <outline text="Ars again" type="rss" xmlUrl="https://ars.example.com/feed"/>
    </outline>
    <outline text="News">
      <outline text="BBC" xmlUrl="https://bbc.example.com/rss"/>
    </outline>
    <outline text="Empty folder"/>
    <outline text="No url outline that is not a folder"/>
  </body>
</opml>
"""


def test_parse_opml_flattens_nested_outlines():
    outlines = parse_opml(OPML)
    assert [(o.xml_url, o.folder, o.title) for o in outlines] == [
        ("https://top.example.com/rss", None, "Top Feed Title"),
        ("https://ars.example.com/feed", "Tech", "Ars"),
        ("https://deep.example.com/atom.xml", "Tech", "Deep"),  # nested folder collapsed into top-level
        ("https://ars.example.com/feed", "Tech", "Ars again"),
        ("https://bbc.example.com/rss", "News", "BBC"),
    ]
    assert outlines[0].html_url == "https://top.example.com/"


def test_parse_opml_rejects_non_opml():
    with pytest.raises(OPMLError):
        parse_opml(b"<html><body>no</body></html>")
    with pytest.raises(OPMLError):
        parse_opml(b"<opml version='2.0'><head/></opml>")
    with pytest.raises(OPMLError):
        parse_opml(b"")


async def test_import_opml_creates_folders_and_skips_duplicates(session, user):
    existing = models.Feed(user_id=user.id, url="https://bbc.example.com/rss", title="BBC old")
    session.add(existing)
    await session.commit()
    before = datetime.now(UTC)

    result = await import_opml(session, user, OPML)
    await session.commit()

    assert sorted(f.url for f in result.added) == [
        "https://ars.example.com/feed",
        "https://deep.example.com/atom.xml",
        "https://top.example.com/rss",
    ]
    assert result.skipped == ["https://ars.example.com/feed", "https://bbc.example.com/rss"]
    # "News" only held the duplicate BBC feed, so no empty folder is created for it
    assert [f.name for f in result.folders_created] == ["Tech"]

    folders = {f.name: f for f in (await session.scalars(select(models.Folder).where(models.Folder.user_id == user.id))).all()}
    assert set(folders) == {"Tech"}
    feeds = {f.url: f for f in (await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all()}
    assert len(feeds) == 4
    assert feeds["https://top.example.com/rss"].folder_id is None
    assert feeds["https://top.example.com/rss"].title == "Top Feed Title"
    assert feeds["https://top.example.com/rss"].site_url == "https://top.example.com/"
    assert feeds["https://ars.example.com/feed"].folder_id == folders["Tech"].id
    assert feeds["https://deep.example.com/atom.xml"].folder_id == folders["Tech"].id
    for feed in result.added:
        assert feed.next_fetch_at is not None and feed.next_fetch_at >= before  # due now, never fetched
        assert feed.last_fetch_at is None
    assert (await session.scalar(select(models.Item.id))) is None  # nothing was fetched

    # Importing again adds nothing and reuses folders
    again = await import_opml(session, user, OPML)
    assert again.added == [] and again.folders_created == []
    assert len(again.skipped) == 5


async def test_export_opml_roundtrip(session, user):
    result = await import_opml(session, user, OPML)
    await session.commit()
    feeds = list((await session.scalars(select(models.Feed).where(models.Feed.user_id == user.id))).all())
    folders = list((await session.scalars(select(models.Folder).where(models.Folder.user_id == user.id))).all())
    assert len(feeds) == 4 and len(folders) == 2

    xml = export_opml(user, feeds, folders)
    assert isinstance(xml, bytes)
    assert xml.startswith(b"<?xml") and b'<opml version="2.0">' in xml
    assert user.email.encode() in xml
    assert b'xmlUrl="https://top.example.com/rss"' in xml and b'htmlUrl="https://top.example.com/"' in xml

    outlines = parse_opml(xml)
    exported = sorted((o.xml_url, o.folder) for o in outlines)
    assert exported == [
        ("https://ars.example.com/feed", "Tech"),
        ("https://bbc.example.com/rss", "News"),
        ("https://deep.example.com/atom.xml", "Tech"),
        ("https://top.example.com/rss", None),
    ]
    assert {o.title for o in outlines if o.folder is None} == {"Top Feed Title"}

    # Round trip into a fresh user reproduces the same structure
    other = models.User(email="other@example.com", password_hash="x")
    session.add(other)
    await session.commit()
    imported = await import_opml(session, other, xml)
    assert len(imported.added) == 4 and sorted(f.name for f in imported.folders_created) == ["News", "Tech"]
    assert imported.skipped == []
    assert all(f.user_id == other.id for f in imported.added)
    assert result.added != imported.added


def test_export_opml_escapes_and_handles_unfiled(user):
    feed = models.Feed(user_id=user.id, url="https://x.example.com/rss?a=1&b=2", title='Quote "me" & co')
    xml = export_opml(user, [feed], [])
    assert b"&amp;b=2" in xml and b"&quot;me&quot;" in xml
    assert parse_opml(xml)[0].title == 'Quote "me" & co'
