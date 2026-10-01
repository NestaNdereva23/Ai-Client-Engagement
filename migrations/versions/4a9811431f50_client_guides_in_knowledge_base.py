"""client guides in the knowledge base

Revision ID: 4a9811431f50
Revises: 8a329564c6d6
Create Date: 2026-10-01 14:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4a9811431f50"
down_revision: str | Sequence[str] | None = "8a329564c6d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rag_documents",
        sa.Column("doc_type", sa.Text(), server_default="report", nullable=False),
    )
    op.create_check_constraint(
        "ck_rag_documents_doc_type",
        "rag_documents",
        "doc_type IN ('report', 'client_guide')",
    )
    op.add_column("rag_document_versions", sa.Column("approved_by", sa.Text(), nullable=True))
    op.add_column(
        "rag_document_versions",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM rag_chunks WHERE version_id IN (
            SELECT v.version_id FROM rag_document_versions v
            JOIN rag_documents d ON d.doc_id = v.doc_id
            WHERE d.doc_type = 'client_guide'
        )
        """
    )
    op.execute(
        """
        DELETE FROM rag_document_versions WHERE doc_id IN (
            SELECT doc_id FROM rag_documents WHERE doc_type = 'client_guide'
        )
        """
    )
    op.execute("DELETE FROM rag_documents WHERE doc_type = 'client_guide'")
    op.drop_column("rag_document_versions", "approved_at")
    op.drop_column("rag_document_versions", "approved_by")
    op.drop_constraint("ck_rag_documents_doc_type", "rag_documents", type_="check")
    op.drop_column("rag_documents", "doc_type")
