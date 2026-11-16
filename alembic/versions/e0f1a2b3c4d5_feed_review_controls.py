"""Reader-controlled essential and quiet subscriptions."""

import sqlalchemy as sa

from alembic import op

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("feeds", sa.Column("essential", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("feeds", sa.Column("delivery", sa.String(10), server_default="normal", nullable=False))


def downgrade():
    op.drop_column("feeds", "delivery")
    op.drop_column("feeds", "essential")
