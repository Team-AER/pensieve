"""saved links and page archive: feeds.kind, items.archive_text (searched), snapshots and archive assets.

Revision ID: d4e8a1b6c2f0
Revises: c7d2e5f1a9b3
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4e8a1b6c2f0"
down_revision = "c7d2e5f1a9b3"
branch_labels = None
depends_on = None

OLD_SEARCH_VECTOR = (
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(content_text, '')), 'B')"
)
NEW_SEARCH_VECTOR = OLD_SEARCH_VECTOR + " || setweight(to_tsvector('english', coalesce(archive_text, '')), 'C')"


def _replace_search_vector(expression: str) -> None:
    # A stored generated column's expression can't be altered in place: drop and re-add (one table rewrite).
    op.drop_index("ix_items_search", table_name="items")
    op.drop_column("items", "search_vector")
    op.add_column(
        "items",
        sa.Column("search_vector", postgresql.TSVECTOR(), sa.Computed(expression, persisted=True), nullable=True),
    )
    op.create_index("ix_items_search", "items", ["search_vector"], postgresql_using="gin")


def upgrade() -> None:
    op.add_column("feeds", sa.Column("kind", sa.String(length=16), nullable=False, server_default="rss"))
    op.create_index(
        "uq_feed_user_saved", "feeds", ["user_id"], unique=True, postgresql_where=sa.text("kind = 'saved'")
    )
    op.add_column("items", sa.Column("archive_text", sa.Text(), nullable=True))
    _replace_search_vector(NEW_SEARCH_VECTOR)

    op.create_table(
        "snapshots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.UUID(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_url", sa.String(length=2048), nullable=False),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=120), nullable=True),
        sa.Column("render_mode", sa.String(length=16), nullable=True),
        sa.Column("site_name", sa.String(length=300), nullable=True),
        sa.Column("byline", sa.String(length=300), nullable=True),
        sa.Column("lang", sa.String(length=20), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("page_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lead_image_sha", sa.String(length=64), nullable=True),
        sa.Column("icon_sha", sa.String(length=64), nullable=True),
        sa.Column("raw_key", sa.String(length=512), nullable=True),
        sa.Column("page_key", sa.String(length=512), nullable=True),
        sa.Column("shot_key", sa.String(length=512), nullable=True),
        sa.Column("file_key", sa.String(length=512), nullable=True),
        sa.Column("bytes_total", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "item_id", name="uq_snapshot_user_item"),
    )
    op.create_index("ix_snapshots_user_id", "snapshots", ["user_id"])
    op.create_index("ix_snapshots_item_id", "snapshots", ["item_id"])
    op.create_table(
        "archive_assets",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "snapshot_assets",
        sa.Column("snapshot_id", sa.UUID(), sa.ForeignKey("snapshots.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "sha256", sa.String(length=64), sa.ForeignKey("archive_assets.sha256", ondelete="CASCADE"), primary_key=True
        ),
    )
    op.create_index("ix_snapshot_assets_sha256", "snapshot_assets", ["sha256"])


def downgrade() -> None:
    op.drop_table("snapshot_assets")
    op.drop_table("archive_assets")
    op.drop_table("snapshots")
    _replace_search_vector(OLD_SEARCH_VECTOR)
    op.drop_column("items", "archive_text")
    op.drop_index("uq_feed_user_saved", table_name="feeds")
    op.drop_column("feeds", "kind")
