"""Pensieve schema. Every user-owned table carries user_id so multi-user is a permissions change later.

This module is the shared contract between the fetcher, web UI, sync API and AI packages.
Do not add columns ad hoc: propose them, then add a migration under alembic/versions.
"""

from __future__ import annotations

import enum
import os
import threading
import time
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pensieve.db import Base

EMBEDDING_DIMS = 768

#: items.search_vector: title (A), feed or saved-page text (B), archived page text of a feed item (C).
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(content_text, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(archive_text, '')), 'C')"
)

#: Expression index on items(id) equal to ``syncapi.common.long_id_sql(Item.id)``: the sync APIs' int64 item id.
LONG_ID_INDEX_SQL = "((('x'||substr(replace(id::text,'-',''),1,16))::bit(64)::bigint) & 9223372036854775807)"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


_uuid7_lock = threading.Lock()
_uuid7_last_ms = -1
_uuid7_sequence = 0


def _uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7). Items use it so ids derived from the first 8 bytes
    (the sync APIs' int64 item ids) increase over time and clients' since_id paging is correct.

    UUIDv7 only leaves 12 bits between its millisecond timestamp and the variant bits. A random value in
    that space can collide after conversion to the sync APIs' 63-bit id. Use a process-local monotonic
    sequence instead, advancing the logical millisecond if all 4096 values are consumed.
    """
    global _uuid7_last_ms, _uuid7_sequence

    with _uuid7_lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _uuid7_last_ms:
            _uuid7_last_ms = now_ms
            _uuid7_sequence = 0
        else:
            _uuid7_sequence += 1
            if _uuid7_sequence > 0xFFF:
                _uuid7_last_ms += 1
                _uuid7_sequence = 0
        ms = _uuid7_last_ms
        sequence = _uuid7_sequence

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (sequence << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    admin = "admin"
    reader = "reader"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.reader, nullable=False
    )
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Free-form per-user settings: theme, density, digest_time, keyboard layout, ai toggles."""

    feeds: Mapped[list[Feed]] = relationship(back_populates="user", cascade="all, delete-orphan")
    folders: Mapped[list[Folder]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiToken(TimestampMixin, Base):
    """Credentials for sync clients (Google Reader API, Fever API)."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    """SHA-256 hex of the plaintext token (kind greader/web). For kind 'fever' it is md5("email:plaintext"),
    which is what the Fever protocol's api_key is; see pensieve/syncapi/fever.py. Plaintext is shown once."""
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="greader")
    """'greader' | 'fever' | 'web'."""
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class Folder(TimestampMixin, Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_folder_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_suggested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="folders")
    feeds: Mapped[list[Feed]] = relationship(back_populates="folder", foreign_keys="Feed.folder_id")


#: ``Feed.kind`` values. A 'saved' feed is the per-user home of links saved with Save link: never polled,
#: hidden from feed management and OPML, shown in the reader as "Saved".
FEED_KIND_RSS = "rss"
FEED_KIND_SAVED = "saved"
SAVED_FEED_URL = "pensieve:saved"


class Feed(TimestampMixin, Base):
    __tablename__ = "feeds"
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_feed_user_url"),
        Index("uq_feed_user_saved", "user_id", unique=True, postgresql_where=text("kind = 'saved'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    site_url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    icon_url: Mapped[str | None] = mapped_column(String(2048))
    icon_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    """Cached favicon bytes (served at /favicons/{feed_id}); refreshed weekly by the fetcher."""
    icon_content_type: Mapped[str | None] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # AI folder suggestion for feeds still in Inbox (folder_id NULL)
    suggested_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL")
    )
    suggested_folder_name: Mapped[str | None] = mapped_column(String(120))
    """A brand-new folder the filer proposes (no Folder row yet); accepting it creates the folder."""
    suggested_folder_confidence: Mapped[float | None] = mapped_column(Float)

    # Fetch state
    fetch_interval_min: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(128))
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    websub_hub: Mapped[str | None] = mapped_column(String(2048))
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FEED_KIND_RSS, server_default=FEED_KIND_RSS
    )
    """'rss' (a subscription) or 'saved' (the user's saved links; see pensieve/archive)."""

    user: Mapped[User] = relationship(back_populates="feeds")
    folder: Mapped[Folder | None] = relationship(back_populates="feeds", foreign_keys=[folder_id])
    items: Mapped[list[Item]] = relationship(back_populates="feed", cascade="all, delete-orphan")


class FeedRule(TimestampMixin, Base):
    """Per-feed (or global when feed_id NULL) filters: hide, star, tag by keyword or regex."""

    __tablename__ = "feed_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feed_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(
        String(20), nullable=False, default="title"
    )  # title | body | author | url
    pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # hide | star | tag | mark_read
    action_value: Mapped[str | None] = mapped_column(String(120))  # tag name for 'tag'
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ---------------------------------------------------------------------------
# Items and reader state
# ---------------------------------------------------------------------------


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_item_feed_guid"),
        Index("ix_items_feed_published", "feed_id", "published_at"),
        Index("ix_items_published", "published_at"),
        Index("ix_items_hash", "hash"),
        Index("ix_items_search", "search_vector", postgresql_using="gin"),
        # Must match pensieve.syncapi.common.long_id_sql() exactly so the planner uses it for since_id/max_id.
        Index("ix_items_long_id", text(LONG_ID_INDEX_SQL)),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid7)
    feed_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    guid: Mapped[str] = mapped_column(String(2048), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    author: Mapped[str | None] = mapped_column(String(300))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set when a known guid's content hash changes and the row is updated in place."""
    content_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """Sanitised feed HTML."""
    content_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """Plain text of content_html, for search and prompts."""
    reader_html: Mapped[str | None] = mapped_column(Text)
    """Reader-mode extraction of the linked page, filled lazily."""
    reader_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 of normalised title + content_text, for exact dedup across feeds."""
    enclosure_url: Mapped[str | None] = mapped_column(String(2048))
    enclosure_type: Mapped[str | None] = mapped_column(String(100))
    archive_text: Mapped[str | None] = mapped_column(Text)
    """Full text of the archived page for a feed item (starred items are archived). Saved links keep their
    full text in content_text instead, so this stays NULL for them."""
    search_vector = mapped_column(
        TSVECTOR,
        Computed(SEARCH_VECTOR_SQL, persisted=True),
    )

    feed: Mapped[Feed] = relationship(back_populates="items")

    @property
    def full_text(self) -> str:
        """The richest text Pensieve has: the archived page when it beats the feed's excerpt."""
        archived = self.archive_text or ""
        own = self.content_text or ""
        return archived if len(archived) > len(own) else own


class ItemState(Base):
    """Per-user reader state. Absent row == unread, unstarred."""

    __tablename__ = "item_states"
    __table_args__ = (
        Index("ix_item_states_user_unread", "user_id", "is_read"),
        Index("ix_item_states_user_starred", "user_id", "is_starred"),
        Index(
            "ix_item_states_user_unread_partial",
            "user_id",
            "item_id",
            postgresql_where=text("is_read = false AND hidden = false"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Set by a FeedRule 'hide' action."""
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list, nullable=False)
    """User-applied tag names (distinct from AI tags in item_ai)."""
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tag(TimestampMixin, Base):
    """Controlled vocabulary per user. kind='ai' rows are the tagging prompt's label set."""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tag_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="ai")  # ai | user
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Note(TimestampMixin, Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")


# ---------------------------------------------------------------------------
# Page archive (saved links, starred items): metadata here, blobs in the S3 store
# ---------------------------------------------------------------------------


class Snapshot(TimestampMixin, Base):
    """One archived capture of an item's page per user; a re-capture replaces it in place.

    Blob keys live under ``snap/<id>/<generation>/`` in the bucket: ``raw`` (server HTML, gzip), ``page``
    (the frozen rendered DOM, gzip), ``shot`` (full-page JPEG) and ``file`` (a PDF or other non-HTML body).
    Images, fonts and the site icon are content-addressed :class:`ArchiveAsset` rows shared across captures.
    """

    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_snapshot_user_item"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    """queued | rendering | done | failed"""
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    requested_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    final_url: Mapped[str | None] = mapped_column(String(2048))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(120))
    render_mode: Mapped[str | None] = mapped_column(String(16))
    """browser (rendered in Chromium) | http (server HTML only) | client (DOM sent by the extension) | file"""
    site_name: Mapped[str | None] = mapped_column(String(300))
    byline: Mapped[str | None] = mapped_column(String(300))
    lang: Mapped[str | None] = mapped_column(String(20))
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lead_image_sha: Mapped[str | None] = mapped_column(String(64))
    icon_sha: Mapped[str | None] = mapped_column(String(64))
    raw_key: Mapped[str | None] = mapped_column(String(512))
    page_key: Mapped[str | None] = mapped_column(String(512))
    shot_key: Mapped[str | None] = mapped_column(String(512))
    file_key: Mapped[str | None] = mapped_column(String(512))
    bytes_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    """Blob bytes of this capture (raw + page + shot + file + its assets' sizes at capture time)."""
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveAsset(Base):
    """A content-addressed blob (image, font, icon) at ``assets/<sha[:2]>/<sha>`` in the bucket."""

    __tablename__ = "archive_assets"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SnapshotAsset(Base):
    """Which assets a snapshot uses: drives the ownership check on /archive/a/<sha> and garbage collection."""

    __tablename__ = "snapshot_assets"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(
        ForeignKey("archive_assets.sha256", ondelete="CASCADE"), primary_key=True, index=True
    )


# ---------------------------------------------------------------------------
# AI outputs (droppable and regenerable without touching reader state)
# ---------------------------------------------------------------------------


class ItemAI(Base):
    """Categorization output per item per user (tags are per-user vocabularies)."""

    __tablename__ = "item_ai"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    content_type: Mapped[str | None] = mapped_column(String(30))
    """article | release_note | tutorial | opinion | announcement | listicle | podcast | video"""
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list, nullable=False)
    confidences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # {tag: 0.0-1.0}
    summary: Mapped[str | None] = mapped_column(Text)
    """On-demand 3-bullet summary + why-it-matters; never generated eagerly."""
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = (
        Index(
            "ix_embeddings_vector_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    vector = mapped_column(Vector(EMBEDDING_DIMS), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Cluster(TimestampMixin, Base):
    __tablename__ = "clusters"
    __table_args__ = (Index("ix_clusters_user_window", "user_id", "window_end"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    headline: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    source_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="story"
    )  # story | duplicate | series

    items: Mapped[list[ClusterItem]] = relationship(back_populates="cluster", cascade="all, delete-orphan")


class ClusterItem(Base):
    __tablename__ = "cluster_items"

    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    similarity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    cluster: Mapped[Cluster] = relationship(back_populates="items")


class ClusterOverride(Base):
    """User corrections to grouping, honoured forever: never re-merge a split pair."""

    __tablename__ = "cluster_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_a: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    item_b: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # merge | split
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Correction(Base):
    """Any user override of an AI output; signal for prompt few-shot refresh and the profile."""

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # item_tag | feed_folder | content_type | cluster
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Profile(Base):
    """Reader memory: a versioned, user-editable plain-text profile."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    diff_from_previous: Mapped[str | None] = mapped_column(Text)
    edited_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Insight(Base):
    """Generated digests, weekly reviews and trend snapshots."""

    __tablename__ = "insights"
    __table_args__ = (UniqueConstraint("user_id", "kind", "period", name="uq_insight_user_kind_period"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # daily | weekly | trend | source_signals
    period: Mapped[str] = mapped_column(String(20), nullable=False)  # 2026-09-18 | 2026-W38
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Structured payload: {summary, top_stories:[{cluster_id, title, why, sources, affinity}], safe_to_skip, ...}"""
    item_refs: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, nullable=False
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppSetting(Base):
    """Install-wide key/value settings edited from the UI (e.g. which gateway models to use).

    Values are JSON; ``pensieve.ai.model_choice`` owns the ``llm`` key.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AIJob(Base):
    """Mirror of queued AI work for observability and replay; the live queue is Redis/arq."""

    __tablename__ = "ai_jobs"
    __table_args__ = (Index("ix_ai_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # embed | tag | cluster | digest | profile | file_feed
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    """queued|running|done|failed|partial (partial: some steps failed on the last attempt)."""
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "EMBEDDING_DIMS",
    "LONG_ID_INDEX_SQL",
    "AIJob",
    "ApiToken",
    "AppSetting",
    "Cluster",
    "ClusterItem",
    "ClusterOverride",
    "Correction",
    "Embedding",
    "Feed",
    "FeedRule",
    "Folder",
    "Insight",
    "Item",
    "ItemAI",
    "ItemState",
    "Note",
    "Profile",
    "Tag",
    "User",
    "UserRole",
]

# Ensure the vector extension exists before tables are created via metadata (tests / dev).
Base.metadata.info["pre_create_sql"] = [text("CREATE EXTENSION IF NOT EXISTS vector")]
