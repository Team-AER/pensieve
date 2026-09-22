"""items.updated_at, feed favicon bytes + suggested_folder_name, unread partial index, long-id index, HNSW

Revision ID: b3f1c9a7d2e4
Revises: 842eb4e75246
Create Date: 2026-09-18 02:40:00

Note: existing items keep their random (v4) ids; new items get time-ordered v7 ids so the sync APIs'
since_id/max_id paging is correct from here on. Ids are not rewritten (item_states, clusters, notes and
client caches reference them); old rows simply sort before new ones in long-id order.
"""

import sqlalchemy as sa

from alembic import op

revision = "b3f1c9a7d2e4"
down_revision = "842eb4e75246"
branch_labels = None
depends_on = None

LONG_ID_INDEX_SQL = "((('x'||substr(replace(id::text,'-',''),1,16))::bit(64)::bigint) & 9223372036854775807)"


def upgrade() -> None:
    op.add_column("items", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("feeds", sa.Column("icon_data", sa.LargeBinary(), nullable=True))
    op.add_column("feeds", sa.Column("icon_content_type", sa.String(length=100), nullable=True))
    op.add_column("feeds", sa.Column("suggested_folder_name", sa.String(length=120), nullable=True))
    # item_states.read_at already exists in the initial schema.
    op.create_index(
        "ix_item_states_user_unread_partial",
        "item_states",
        ["user_id", "item_id"],
        unique=False,
        postgresql_where=sa.text("is_read = false AND hidden = false"),
    )
    op.create_index("ix_items_long_id", "items", [sa.text(LONG_ID_INDEX_SQL)], unique=False)
    op.create_index(
        "ix_embeddings_vector_hnsw",
        "embeddings",
        ["vector"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"vector": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_embeddings_vector_hnsw", table_name="embeddings")
    op.drop_index("ix_items_long_id", table_name="items")
    op.drop_index("ix_item_states_user_unread_partial", table_name="item_states")
    op.drop_column("feeds", "suggested_folder_name")
    op.drop_column("feeds", "icon_content_type")
    op.drop_column("feeds", "icon_data")
    op.drop_column("items", "updated_at")
