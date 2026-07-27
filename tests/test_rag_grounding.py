"""Grounding rate claims against retrieved chunks.

Pure tests over hand-built chunks check that a cited rate traces to a chunk, a
fabricated rate is flagged, placeholders are ignored, and enforcement fails
closed. A database smoke test seeds two weeks, confirms the latest is served,
and grounds a real and a fabricated draft against what retrieval returns.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal
from app.rag.embedding import HashingEmbedder
from app.rag.grounding import UngroundedClaim, check_grounding, enforce_grounding
from app.rag.ingest import ingest_report
from app.rag.retrieve import retrieve_product_facts

EMB = HashingEmbedder()


def _chunk(chunk_id: int, text: str):
    return SimpleNamespace(chunk_id=chunk_id, text=text)


CHUNKS = [
    _chunk(101, "Cytonn Money Market Fund closed the week at a yield of 11.35% p.a."),
    _chunk(102, "T-bills were oversubscribed with the 91-day paper yielding 8.78%."),
]


# --- pure grounding ---------------------------------------------------------


def test_a_cited_rate_traces_to_its_chunk() -> None:
    result = check_grounding("The fund yield is now 11.35% per annum.", CHUNKS)
    assert result.ok
    assert result.claims[0].value == "11.35%"
    assert result.claims[0].chunk_ids == [101]


def test_a_fabricated_rate_is_flagged() -> None:
    result = check_grounding("Enjoy a guaranteed 25% return today.", CHUNKS)
    assert not result.ok
    assert result.unsupported == ["25%"]
    assert result.claims[0].chunk_ids == []


def test_a_draft_with_no_rate_claims_is_grounded() -> None:
    result = check_grounding("We would love to welcome you back to the fund.", CHUNKS)
    assert result.ok
    assert result.claims == []


def test_placeholders_are_not_treated_as_claims() -> None:
    result = check_grounding("Your yield could be {{yield}} this week.", CHUNKS)
    assert result.ok
    assert result.claims == []


def test_mixed_claims_report_each_outcome() -> None:
    result = check_grounding("Between 8.78% and a fanciful 40%.", CHUNKS)
    assert not result.ok
    supported = {c.value: c.supported for c in result.claims}
    assert supported == {"8.78%": True, "40%": False}


def test_enforce_raises_only_on_an_unsupported_claim() -> None:
    enforce_grounding("A steady 11.35% p.a.", CHUNKS)  # grounded, no raise
    with pytest.raises(UngroundedClaim, match="99.9%"):
        enforce_grounding("An impossible 99.9% return.", CHUNKS)


# --- database smoke over the real pipeline ----------------------------------

SOURCE = "test-grounding"
_WEEK_29 = ["Investment Updates: Cytonn Money Market Fund closed at a yield of 11.35% p.a."]
_WEEK_30 = ["Investment Updates: Cytonn Money Market Fund closed at a yield of 11.60% p.a."]


def _purge() -> None:
    with SessionLocal() as session:
        doc = session.scalar(select(RagDocument).where(RagDocument.source == SOURCE))
        if not doc:
            return
        vids = session.scalars(
            select(RagDocumentVersion.version_id).where(RagDocumentVersion.doc_id == doc.doc_id)
        ).all()
        session.execute(delete(RagChunk).where(RagChunk.version_id.in_(vids)))
        session.execute(delete(RagDocumentVersion).where(RagDocumentVersion.doc_id == doc.doc_id))
        session.execute(delete(RagDocument).where(RagDocument.doc_id == doc.doc_id))
        session.commit()


@pytest.fixture
def two_weeks(db: None):
    _purge()
    for pages, issue in [(_WEEK_29, "29/2026"), (_WEEK_30, "30/2026")]:
        with SessionLocal() as session:
            ingest_report(
                session,
                pages=pages,
                issue=issue,
                document_title="Cytonn Weekly Report",
                document_source=SOURCE,
                embedder=EMB,
            )
    yield
    _purge()


def test_latest_week_is_served_and_grounds_a_true_draft(two_weeks) -> None:
    with SessionLocal() as session:
        hits = retrieve_product_facts(session, product="Money Market Fund", embedder=EMB)
    # Retrieval serves the latest week.
    assert "11.60%" in hits[0].text
    # A draft citing the served yield is grounded; a stale or fabricated one is not.
    assert check_grounding("The fund now yields 11.60% p.a.", hits).ok
    assert not check_grounding("The fund now yields 11.35% p.a.", hits).ok
    assert not check_grounding("The fund now yields 30% p.a.", hits).ok
