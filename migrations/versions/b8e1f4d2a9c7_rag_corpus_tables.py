"""rag_documents, rag_document_versions, rag_chunks with a vector index

Revision ID: b8e1f4d2a9c7
Revises: a3f7e2c9b8d1
Create Date: 2026-07-25 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8e1f4d2a9c7"
down_revision: str | Sequence[str] | None = "a3f7e2c9b8d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match EMBEDDING_DIM in app/db/models/rag.py.
EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.create_table(
        "rag_documents",
        sa.Column("doc_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("doc_id"),
    )

    op.create_table(
        "rag_document_versions",
        sa.Column("version_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("published_on", sa.Date(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["doc_id"], ["rag_documents.doc_id"]),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("doc_id", "version_no", name="uq_rag_version_doc_no"),
    )
    op.create_index("ix_rag_document_versions_doc_id", "rag_document_versions", ["doc_id"])
    # At most one active version per document.
    op.create_index(
        "uq_rag_active_version_per_doc",
        "rag_document_versions",
        ["doc_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "rag_chunks",
        sa.Column("chunk_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["version_id"], ["rag_document_versions.version_id"]),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_rag_chunk_version_ordinal"),
    )
    op.create_index("ix_rag_chunks_version_id", "rag_chunks", ["version_id"])
    op.create_index(
        "ix_rag_chunks_embedding_hnsw",
        "rag_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_rag_chunks_embedding_hnsw", table_name="rag_chunks")
    op.drop_index("ix_rag_chunks_version_id", table_name="rag_chunks")
    op.drop_table("rag_chunks")
    op.drop_index("uq_rag_active_version_per_doc", table_name="rag_document_versions")
    op.drop_index("ix_rag_document_versions_doc_id", table_name="rag_document_versions")
    op.drop_table("rag_document_versions")
    op.drop_table("rag_documents")
