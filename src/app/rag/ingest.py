from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models.rag import RagDocument, RagDocumentVersion
from app.rag.chunking import chunk_report
from app.rag.embedding import Embedder
from app.rag.index import index_chunks
from app.rag.loader import load_pdf


@dataclass(frozen=True)
class IngestResult:
    doc_id: int
    version_id: int
    version_no: int
    chunks: int
    created: bool  # True for a new version, False when an existing issue was refreshed


def _document(session: Session, *, title: str, source: str) -> RagDocument:
    doc = session.scalar(select(RagDocument).where(RagDocument.source == source))
    if doc is None:
        doc = RagDocument(title=title, source=source)
        session.add(doc)
        session.flush()
    return doc


def ingest_report(
    session: Session,
    *,
    pages: list[str],
    issue: str,
    document_title: str,
    document_source: str,
    published_on: date | None = None,
    embedder: Embedder | None = None,
) -> IngestResult:
    """Store the report as a new active version, retaining and retiring the old ones.

    Re-running with an issue already on file refreshes that version in place: the
    version number is kept and its chunks are replaced, so no duplicates appear.
    """
    doc = _document(session, title=document_title, source=document_source)

    # The issue tag is the idempotency key for this document.
    version = session.scalar(
        select(RagDocumentVersion).where(
            RagDocumentVersion.doc_id == doc.doc_id,
            RagDocumentVersion.source == issue,
        )
    )
    created = version is None
    if version is None:
        next_no = (
            session.scalar(
                select(func.max(RagDocumentVersion.version_no)).where(
                    RagDocumentVersion.doc_id == doc.doc_id
                )
            )
            or 0
        ) + 1
        version = RagDocumentVersion(
            doc_id=doc.doc_id,
            version_no=next_no,
            source=issue,
            published_on=published_on,
            is_active=False,
        )
        session.add(version)
        session.flush()
    elif published_on is not None:
        version.published_on = published_on
    version_id, version_no = version.version_id, version.version_no

    chunks = chunk_report(pages, issue=issue)
    n = index_chunks(session, version_id, chunks, embedder=embedder)

    # Serve this version and retire the rest, honouring one active version per doc.
    session.execute(
        update(RagDocumentVersion)
        .where(RagDocumentVersion.doc_id == doc.doc_id)
        .values(is_active=False)
    )
    session.execute(
        update(RagDocumentVersion)
        .where(RagDocumentVersion.version_id == version_id)
        .values(is_active=True)
    )
    session.commit()
    return IngestResult(doc.doc_id, version_id, version_no, n, created)


def ingest_report_pdf(
    session: Session,
    path: str,
    *,
    document_title: str = "Cytonn Weekly Report",
    document_source: str = "cytonn-weekly",
    embedder: Embedder | None = None,
) -> IngestResult:
    """Load an uploaded report PDF and ingest it as the new active version."""
    doc = load_pdf(path)
    if not doc.issue:
        raise ValueError("could not read an issue tag from the report; cannot version it")
    return ingest_report(
        session,
        pages=doc.pages,
        issue=doc.issue,
        document_title=document_title,
        document_source=document_source,
        published_on=doc.published_on,
        embedder=embedder,
    )
