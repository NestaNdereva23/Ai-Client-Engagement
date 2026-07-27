"""Embed a version's chunks and store the vectors.

Re-indexing a version replaces its chunks, so running twice leaves no
duplicates. The configured embedder is used unless one is injected for tests.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.rag import RagChunk
from app.rag.chunking import ReportChunk
from app.rag.embedding import Embedder, embed_batched, get_embedder


def index_chunks(
    session: Session,
    version_id: int,
    chunks: list[ReportChunk],
    *,
    embedder: Embedder | None = None,
    batch_size: int | None = None,
) -> int:
    """Embed the chunks and store them under one version, replacing any prior set."""
    embedder = embedder or get_embedder()
    batch_size = batch_size or get_settings().embedding_batch_size

    session.execute(delete(RagChunk).where(RagChunk.version_id == version_id))
    if not chunks:
        session.commit()
        return 0

    vectors = embed_batched(embedder, [c.text for c in chunks], batch_size)
    session.add_all(
        RagChunk(
            version_id=version_id,
            ordinal=chunk.ordinal,
            text=chunk.text,
            chunk_metadata=chunk.metadata,
            embedding=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    )
    session.commit()
    return len(chunks)
