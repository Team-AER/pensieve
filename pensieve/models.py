"""Pensieve schema. Every user-owned table carries user_id so multi-user is a permissions change later.

This module is the shared contract between the fetcher, web UI, sync API and AI packages.
Do not add columns ad hoc: propose them, then add a migration under alembic/versions.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pensieve.db import Base

EMBEDDING_DIMS = 768


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
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
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), default=UserRole.reader, nullable=False)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Free-form per-user settings: theme, density, digest_time, keyboard layout, ai toggles."""

    feeds: Mapped[list[Feed]] = relationship(back_populates="user", cascade="all, delete-orphan")
    folders: Mapped[list[Folder]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiToken(TimestampMixin, Base):
    """Credentials for sync clients (Google Reader API, Fever API)."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    """SHA-256 hex of the plaintext token. Plaintext is shown once at creation."""
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
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_suggested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="folders")
    feeds: Mapped[list[Feed]] = relationship(back_populates="folder", foreign_keys="Feed.folder_id")


class Feed(TimestampMixin, Base):
    __tablename__ = "feeds"
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_feed_user_url"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    site_url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    icon_url: Mapped[str | None] = mapped_column(String(2048))
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # AI folder suggestion for feeds still in Inbox (folder_id NULL)
    suggested_folder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"))
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

    user: Mapped[User] = relationship(back_populates="feeds")
    folder: Mapped[Folder | None] = relationship(back_populates="feeds", foreign_keys=[folder_id])
    items: Mapped[list[Item]] = relationship(back_populates="feed", cascade="all, delete-orphan")


class FeedRule(TimestampMixin, Base):
    """Per-feed (or global when feed_id NULL) filters: hide, star, tag by keyword or regex."""

    __tablename__ = "feed_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    feed_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(20), nullable=False, default="title")  # title | body | author | url
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
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    feed_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    guid: Mapped[str] = mapped_column(String(2048), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    author: Mapped[str | None] = mapped_column(String(300))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
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
    search_vector = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(content_text, '')), 'B')",
            persisted=True,
        ),
    )

    feed: Mapped[Feed] = relationship(back_populates="items")


class ItemState(Base):
    """Per-user reader state. Absent row == unread, unstarred."""

    __tablename__ = "item_states"
    __table_args__ = (
        Index("ix_item_states_user_unread", "user_id", "is_read"),
        Index("ix_item_states_user_starred", "user_id", "is_starred"),
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
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="ai")  # ai | user
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Note(TimestampMixin, Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")


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
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Embedding(Base):
    __tablename__ = "embeddings"

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    vector = mapped_column(Vector(EMBEDDING_DIMS), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="story")  # story | duplicate | series

    items: Mapped[list[ClusterItem]] = relationship(back_populates="cluster", cascade="all, delete-orphan")


class ClusterItem(Base):
    __tablename__ = "cluster_items"

    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True, index=True)
    similarity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    cluster: Mapped[Cluster] = relationship(back_populates="items")


class ClusterOverride(Base):
    """User corrections to grouping, honoured forever: never re-merge a split pair."""

    __tablename__ = "cluster_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    item_a: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    item_b: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # merge | split
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Correction(Base):
    """Any user override of an AI output; signal for prompt few-shot refresh and the profile."""

    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # item_tag | feed_folder | content_type | cluster
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Profile(Base):
    """Reader memory: a versioned, user-editable plain-text profile."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    diff_from_previous: Mapped[str | None] = mapped_column(Text)
    edited_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Insight(Base):
    """Generated digests, weekly reviews and trend snapshots."""

    __tablename__ = "insights"
    __table_args__ = (UniqueConstraint("user_id", "kind", "period", name="uq_insight_user_kind_period"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # daily | weekly | trend | source_signals
    period: Mapped[str] = mapped_column(String(20), nullable=False)  # 2026-09-18 | 2026-W38
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    """Structured payload: {summary, top_stories:[{cluster_id, title, why, sources, affinity}], safe_to_skip, ...}"""
    item_refs: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AIJob(Base):
    """Mirror of queued AI work for observability and replay; the live queue is Redis/arq."""

    __tablename__ = "ai_jobs"
    __table_args__ = (Index("ix_ai_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # embed | tag | cluster | digest | profile | file_feed
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")  # queued|running|done|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


__all__ = [
    "EMBEDDING_DIMS",
    "AIJob",
    "ApiToken",
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
