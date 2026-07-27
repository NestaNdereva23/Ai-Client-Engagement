"""Indexing chunks and retrieving product facts by section and similarity.

Uses the deterministic hashing embedder for both indexing and querying, so a
query embeds the same way its chunk did. Checks that the fund-yield fact is
retrieved for a money-market query, that indexing is idempotent per version, and
that only the active version is served.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal
from app.rag.chunking import ReportChunk
from app.rag.embedding import HashingEmbedder
from app.rag.index import index_chunks
from app.rag.retrieve import retrieve, retrieve_product_facts

EMB = HashingEmbedder()

_ACTIVE_CHUNKS = [
    ReportChunk(
        0,
        "Cytonn Money Market Fund closed the week at a yield of 11.35% p.a.",
        {"section": "Money Markets", "issue": "29/2026", "page": 3},
    ),
    ReportChunk(
        1,
        "The NASI index gained on foreign inflows in the equities market this week.",
        {"section": "Equities", "issue": "29/2026", "page": 11},
    ),
    ReportChunk(
        2,
        "The real estate sector saw new residential supply come up in Nairobi.",
        {"section": "Real Estate", "issue": "29/2026", "page": 17},
    ),
    ReportChunk(
        3,
        "T-bills were oversubscribed with the 91-day paper yielding 8.78%.",
        {"section": "Fixed Income", "issue": "29/2026", "page": 4},
    ),
]


@pytest.fixture
def indexed(db: None):
    """A document with an active and an inactive version, both indexed."""
    with SessionLocal() as session:
        doc = RagDocument(title="Weekly Report", source="test")
        session.add(doc)
        session.flush()
        active = RagDocumentVersion(doc_id=doc.doc_id, version_no=2, is_active=True)
        stale = RagDocumentVersion(doc_id=doc.doc_id, version_no=1, is_active=False)
        session.add_all([active, stale])
        session.flush()
        doc_id, active_id, stale_id = doc.doc_id, active.version_id, stale.version_id
        session.commit()

    with SessionLocal() as session:
        index_chunks(session, active_id, _ACTIVE_CHUNKS, embedder=EMB)
        # An old money-market fact under the retired version.
        index_chunks(
            session,
            stale_id,
            [
                ReportChunk(
                    0,
                    "Last month the money market fund yield was 10.90% p.a.",
                    {"section": "Money Markets", "issue": "25/2026", "page": 3},
                )
            ],
            embedder=EMB,
        )

    yield doc_id, active_id, stale_id

    with SessionLocal() as session:
        session.execute(delete(RagChunk).where(RagChunk.version_id.in_([active_id, stale_id])))
        session.execute(delete(RagDocumentVersion).where(RagDocumentVersion.doc_id == doc_id))
        session.execute(delete(RagDocument).where(RagDocument.doc_id == doc_id))
        session.commit()


def test_retrieves_the_fund_yield_for_a_money_market_query(indexed) -> None:
    with SessionLocal() as session:
        hits = retrieve_product_facts(
            session, product="Money Market Fund", angle="winback_habit", embedder=EMB
        )
    assert hits
    assert "yield of 11.35%" in hits[0].text
    assert hits[0].metadata["section"] == "Money Markets"


def test_section_filter_restricts_the_candidates(indexed) -> None:
    with SessionLocal() as session:
        hits = retrieve(session, "current yield", sections=["Money Markets"], embedder=EMB, k=10)
    assert hits
    assert all(h.metadata["section"] == "Money Markets" for h in hits)


def test_a_query_matching_a_chunk_scores_near_one(indexed) -> None:
    with SessionLocal() as session:
        hits = retrieve(
            session,
            "Cytonn Money Market Fund closed the week at a yield of 11.35% p.a.",
            embedder=EMB,
            k=1,
        )
    assert hits[0].score > 0.99


def test_only_the_active_version_is_served(indexed) -> None:
    _doc_id, active_id, stale_id = indexed
    with SessionLocal() as session:
        active_hits = retrieve(session, "money market fund yield", embedder=EMB, k=10)
        all_hits = retrieve(
            session, "money market fund yield", embedder=EMB, k=10, active_only=False
        )
    assert all(h.version_id == active_id for h in active_hits)
    assert any(h.version_id == stale_id for h in all_hits)


def test_reindexing_a_version_leaves_no_duplicates(indexed) -> None:
    _doc_id, active_id, _stale_id = indexed
    with SessionLocal() as session:
        index_chunks(session, active_id, _ACTIVE_CHUNKS, embedder=EMB)
    with SessionLocal() as session:
        count = session.scalar(
            select(func.count()).select_from(RagChunk).where(RagChunk.version_id == active_id)
        )
    assert count == len(_ACTIVE_CHUNKS)
