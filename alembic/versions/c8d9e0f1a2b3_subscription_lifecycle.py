"""Reversible subscriptions and durable subscription events."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "b5c7d9e1f3a2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("feeds", sa.Column("unfollowed_at", sa.DateTime(timezone=True)))
    op.add_column("feeds", sa.Column("pause_reason", sa.String(10)))
    op.create_index("ix_feeds_unfollowed_at", "feeds", ["unfollowed_at"])
    # Preserve existing user pauses. Saved is a special container, never resumed.
    op.execute("""UPDATE feeds SET pause_reason = CASE
        WHEN last_error LIKE '%410%' THEN 'gone'
        WHEN error_count >= 10 THEN 'errors' ELSE 'user' END
        WHERE paused AND kind != 'saved'""")
    op.create_table(
        "subscription_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feed_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("feeds.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("previous_state", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscription_events_user_id", "subscription_events", ["user_id"])
    op.create_index("ix_subscription_events_feed_id", "subscription_events", ["feed_id"])


def downgrade():
    op.drop_table("subscription_events")
    op.drop_index("ix_feeds_unfollowed_at", "feeds")
    op.drop_column("feeds", "pause_reason")
    op.drop_column("feeds", "unfollowed_at")
