"""Weekly ingest: version the report, serve the latest, stay idempotent.

An upload becomes a new active version; the previous week is retained but retired;
re-uploading the same issue refreshes in place with no duplicate version or chunks.
The optional last test runs a real Cytonn PDF end to end when it is present.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, func, select

from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal
from app.rag.embedding import HashingEmbedder
from app.rag.ingest import ingest_report, ingest_report_pdf
from app.rag.retrieve import retrieve_product_facts

EMB = HashingEmbedder()
SOURCE = "test-weekly"

_WEEK_29 = [
    "Executive Summary Fixed Income: T-bills were oversubscribed this week.",
    "Investment Updates: Cytonn Money Market Fund closed the week at a yield of 11.35% p.a.",
]
_WEEK_30 = [
    "Executive Summary Fixed Income: T-bills stayed oversubscribed this week.",
    "Investment Updates: Cytonn Money Market Fund closed the week at a yield of 11.60% p.a.",
]


def _ingest(pages, issue, source=SOURCE):
    with SessionLocal() as session:
        return ingest_report(
            session,
            pages=pages,
            issue=issue,
            document_title="Cytonn Weekly Report",
            document_source=source,
            embedder=EMB,
        )


def _purge(source: str) -> None:
    with SessionLocal() as session:
        doc = session.scalar(select(RagDocument).where(RagDocument.source == source))
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
def clean(db: None):
    _purge(SOURCE)
    yield
    _purge(SOURCE)


def _active_count(doc_id: int) -> int:
    with SessionLocal() as session:
        return session.scalar(
            select(func.count())
            .select_from(RagDocumentVersion)
            .where(RagDocumentVersion.doc_id == doc_id, RagDocumentVersion.is_active.is_(True))
        )


def test_first_upload_creates_an_active_version(clean) -> None:
    result = _ingest(_WEEK_29, "29/2026")
    assert result.created is True
    assert result.version_no == 1
    assert result.chunks > 0
    assert _active_count(result.doc_id) == 1


def test_new_week_supersedes_but_retains_the_old(clean) -> None:
    first = _ingest(_WEEK_29, "29/2026")
    second = _ingest(_WEEK_30, "30/2026")
    assert second.created is True
    assert second.version_no == 2
    assert second.doc_id == first.doc_id

    # Exactly one active version, and both are retained on file.
    assert _active_count(first.doc_id) == 1
    with SessionLocal() as session:
        versions = session.scalars(
            select(RagDocumentVersion).where(RagDocumentVersion.doc_id == first.doc_id)
        ).all()
        active = [v for v in versions if v.is_active]
    assert len(versions) == 2
    assert active[0].version_id == second.version_id

    # Retrieval serves the latest week's yield, not the retired one.
    with SessionLocal() as session:
        hits = retrieve_product_facts(session, product="Money Market Fund", embedder=EMB)
    assert "11.60%" in hits[0].text


def test_reingesting_the_same_issue_is_idempotent(clean) -> None:
    first = _ingest(_WEEK_29, "29/2026")
    again = _ingest(_WEEK_29, "29/2026")
    assert again.created is False
    assert again.version_id == first.version_id
    assert again.version_no == first.version_no

    with SessionLocal() as session:
        version_count = session.scalar(
            select(func.count())
            .select_from(RagDocumentVersion)
            .where(RagDocumentVersion.doc_id == first.doc_id)
        )
        chunk_count = session.scalar(
            select(func.count())
            .select_from(RagChunk)
            .where(RagChunk.version_id == first.version_id)
        )
    assert version_count == 1
    assert chunk_count == first.chunks
    assert _active_count(first.doc_id) == 1


_SAMPLE_PDF = (
    r"C:\Users\HomePC\Downloads"
    r"\navigating-markets-in-an-era-of-geopolitical-fragmentation-cytonn-weekly-292026-v3.pdf"
)


@pytest.mark.skipif(not os.path.exists(_SAMPLE_PDF), reason="sample report PDF not present")
def test_ingesting_a_real_pdf_end_to_end(db: None) -> None:
    source = "test-weekly-real"
    _purge(source)
    try:
        with SessionLocal() as session:
            result = ingest_report_pdf(session, _SAMPLE_PDF, document_source=source, embedder=EMB)
        assert result.chunks > 20
        with SessionLocal() as session:
            hits = retrieve_product_facts(
                session, product="Money Market Fund", angle="winback_habit", embedder=EMB
            )
        assert hits
        assert "yield" in hits[0].text.lower()
    finally:
        _purge(source)
