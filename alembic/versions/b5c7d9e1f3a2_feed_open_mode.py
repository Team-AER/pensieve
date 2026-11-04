"""feeds.open_mode: per-subscription override for how its items open.

'auto' keeps today's behaviour (judge each entry); 'feed' always shows the entry the feed sent (comics,
photo posts) and 'reader' always opens Reader view (feeds that only tease).

Revision ID: b5c7d9e1f3a2
Revises: a9c4e2f7b3d1
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b5c7d9e1f3a2"
down_revision = "a9c4e2f7b3d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column("open_mode", sa.String(length=10), nullable=False, server_default="auto"),
    )


def downgrade() -> None:
    op.drop_column("feeds", "open_mode")
