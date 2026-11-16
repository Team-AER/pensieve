"""Catalog stream memberships, without a database copy of the catalog."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stream_follows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("chosen_urls", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "slug", name="uq_stream_follow_user_slug"),
    )
    op.create_index("ix_stream_follows_user_id", "stream_follows", ["user_id"])


def downgrade():
    op.drop_table("stream_follows")
