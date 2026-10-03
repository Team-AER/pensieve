"""ai_jobs as a durable ledger: each queued AI job keeps its arq function, args and job id.

Revision ID: f1a2b3c4d5e6
Revises: e8b9c0d1a2f3
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e8b9c0d1a2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_jobs", sa.Column("function", sa.String(length=40), nullable=True))
    op.add_column("ai_jobs", sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("ai_jobs", sa.Column("job_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_jobs", "job_id")
    op.drop_column("ai_jobs", "args")
    op.drop_column("ai_jobs", "function")
