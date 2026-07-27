"""The RAG corpus schema: documents, versioned history, embedded chunks.

Pure checks pin the embedding width and the mapping quirks; the database tests
prove the vector column and its similarity index work, one active version per
document is enforced, and chunks cascade off a version.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models.rag import EMBEDDING_DIM, RagChunk, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal


def _vec(*nonzero: tuple[int, float]) -> list[float]:
    """A zero embedding with a few dimensions set, padded to the model width."""
    v = [0.0] * EMBEDDING_DIM
    for i, val in nonzero:
        v[i] = val
    return v


def test_embedding_column_width_matches_the_constant() -> None:
    assert RagChunk.__table__.c.embedding.type.dim == EMBEDDING_DIM


def test_metadata_is_mapped_around_the_reserved_name() -> None:
    # The attribute is chunk_metadata; the column is metadata.
    assert RagChunk.chunk_metadata.property.columns[0].name == "metadata"


@pytest.fixture
def corpus(db: None):
    """A document with one active version; cleaned up afterwards."""
    with SessionLocal() as session:
        doc = RagDocument(title="Weekly Product Report", source="test-feed")
        session.add(doc)
        session.flush()
        version = RagDocumentVersion(
            doc_id=doc.doc_id, version_no=1, is_active=True, source="week-30"
        )
        session.add(version)
        session.flush()
        doc_id, version_id = doc.doc_id, version.version_id
        session.commit()

    yield doc_id, version_id

    with SessionLocal() as session:
        session.execute(delete(RagChunk).where(RagChunk.version_id == version_id))
        session.execute(delete(RagDocumentVersion).where(RagDocumentVersion.doc_id == doc_id))
        session.execute(delete(RagDocument).where(RagDocument.doc_id == doc_id))
        session.commit()


def test_chunk_round_trips_embedding_and_metadata(corpus) -> None:
    _doc_id, version_id = corpus
    with SessionLocal() as session:
        session.add(
            RagChunk(
                version_id=version_id,
                ordinal=0,
                text="Money Market Fund yield is competitive this week.",
                chunk_metadata={"product": "MMF", "section": "rates"},
                embedding=_vec((0, 1.0)),
            )
        )
        session.commit()

    with SessionLocal() as session:
        chunk = session.scalar(select(RagChunk).where(RagChunk.version_id == version_id))
        assert chunk.chunk_metadata == {"product": "MMF", "section": "rates"}
        assert len(chunk.embedding) == EMBEDDING_DIM
        assert chunk.embedding[0] == 1.0


def test_nearest_neighbour_retrieval_returns_the_closest_chunk(corpus) -> None:
    _doc_id, version_id = corpus
    with SessionLocal() as session:
        session.add_all(
            [
                RagChunk(version_id=version_id, ordinal=1, text="a", embedding=_vec((0, 1.0))),
                RagChunk(version_id=version_id, ordinal=2, text="b", embedding=_vec((1, 1.0))),
                RagChunk(version_id=version_id, ordinal=3, text="c", embedding=_vec((2, 1.0))),
            ]
        )
        session.commit()

    query = _vec((1, 1.0))  # closest to the "b" chunk
    with SessionLocal() as session:
        nearest = session.scalar(
            select(RagChunk)
            .where(RagChunk.version_id == version_id)
            .order_by(RagChunk.embedding.cosine_distance(query))
            .limit(1)
        )
        assert nearest.text == "b"


def test_only_one_active_version_per_document(corpus) -> None:
    doc_id, _version_id = corpus
    with SessionLocal() as session, pytest.raises(IntegrityError):
        # A second active version for the same document must be rejected.
        session.add(
            RagDocumentVersion(doc_id=doc_id, version_no=2, is_active=True, source="week-31")
        )
        session.commit()


def test_a_document_may_keep_many_inactive_versions(corpus) -> None:
    doc_id, _version_id = corpus
    with SessionLocal() as session:
        session.add_all(
            [
                RagDocumentVersion(doc_id=doc_id, version_no=2, is_active=False, source="w31"),
                RagDocumentVersion(doc_id=doc_id, version_no=3, is_active=False, source="w32"),
            ]
        )
        session.commit()
        count = len(
            session.scalars(
                select(RagDocumentVersion).where(RagDocumentVersion.doc_id == doc_id)
            ).all()
        )
    assert count == 3
