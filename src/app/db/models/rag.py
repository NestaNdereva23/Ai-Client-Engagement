"""Retrieval corpus: documents, their weekly versions, and embedded chunks.

Each weekly report is a document with one version per ingest. Old versions are
kept for provenance and audit; only the version flagged is_active is served.
Chunks belong to a version and carry a pgvector embedding for similarity search.
"""

from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Embedding width, tied to the configured embedding model. Changing the model
# means a migration that rebuilds this column.
EMBEDDING_DIM = 1024


class RagDocument(Base):
    __tablename__ = "rag_documents"

    doc_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RagDocumentVersion(Base):
    """One ingest of a document. History is retained; is_active marks the served one."""

    __tablename__ = "rag_document_versions"
    __table_args__ = (
        UniqueConstraint("doc_id", "version_no", name="uq_rag_version_doc_no"),
        # At most one active version per document.
        Index(
            "uq_rag_active_version_per_doc",
            "doc_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rag_documents.doc_id"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RagChunk(Base):
    """One embedded passage of a document version, retrieved by vector similarity."""

    __tablename__ = "rag_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_rag_chunk_version_ordinal"),
        # Cosine HNSW index for nearest-neighbour retrieval.
        Index(
            "ix_rag_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    chunk_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("rag_document_versions.version_id"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Section heading, product, rate, date. "metadata" is reserved on the base,
    # so the attribute is chunk_metadata while the column stays metadata.
    chunk_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
