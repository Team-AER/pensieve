"""Use 1024-dimensional BGE M3 item embeddings.

Old vectors cannot be compared with BGE M3 vectors. They are removed so the
application can regenerate them with the new model.

Revision ID: e8b9c0d1a2f3
Revises: d4e8a1b6c2f0
Create Date: 2026-09-23
"""

from alembic import op

revision = "e8b9c0d1a2f3"
down_revision = "d4e8a1b6c2f0"
branch_labels = None
depends_on = None


def _change_dimensions(dimensions: int) -> None:
    op.drop_index("ix_embeddings_vector_hnsw", table_name="embeddings")
    op.execute("DELETE FROM embeddings")
    op.execute(
        f"ALTER TABLE embeddings ALTER COLUMN vector TYPE vector({dimensions}) "
        f"USING vector::vector({dimensions})"
    )
    op.create_index(
        "ix_embeddings_vector_hnsw",
        "embeddings",
        ["vector"],
        postgresql_using="hnsw",
        postgresql_ops={"vector": "vector_cosine_ops"},
    )


def upgrade() -> None:
    _change_dimensions(1024)


def downgrade() -> None:
    _change_dimensions(768)
