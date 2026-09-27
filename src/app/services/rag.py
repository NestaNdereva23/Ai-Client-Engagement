"""Console reads and admin actions over the RAG corpus, wrapping the M5 ingest.

Nothing here re-implements chunking, embedding, or retrieval; every function
is a thin pass-through to app.rag, the same functions the generation
pipeline itself calls, so a debug search or an admin activation can never
drift from what generation actually sees.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import Row, select, update
from sqlalchemy.orm import Session

from app.db.models.rag import RagDocument, RagDocumentVersion
from app.rag.ingest import IngestResult, ingest_report_pdf
from app.rag.retrieve import Retrieved, retrieve, retrieve_product_facts, sections_for_product


class VersionNotFound(Exception):
    """No rag_document_versions row exists with the given id."""


def ingest_uploaded_report(
    session: Session,
    file_bytes: bytes,
    *,
    document_title: str | None = None,
    document_source: str | None = None,
) -> IngestResult:
    """Save the upload to a temp file and ingest it as the new active version.

    ingest_report_pdf only takes a path, the same as scripts/ingest_report.py
    already uses; writing the upload to disk keeps that module untouched
    rather than teaching it to read from a stream too. document_title and
    document_source default to the one ongoing weekly series when omitted,
    the same default ingest_report_pdf itself uses; overriding them is what
    test_rag_ingest.py's own real-PDF test already does to avoid touching
    that series, so the API exposes the same seam rather than hardcoding it.
    """
    kwargs = {}
    if document_title is not None:
        kwargs["document_title"] = document_title
    if document_source is not None:
        kwargs["document_source"] = document_source

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        return ingest_report_pdf(session, tmp_path, **kwargs)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def list_versions(session: Session) -> list[Row]:
    """Every ingested version across all documents, newest first, with its document's title."""
    query = (
        select(
            RagDocumentVersion.version_id,
            RagDocumentVersion.doc_id,
            RagDocument.title,
            RagDocumentVersion.version_no,
            RagDocumentVersion.source,
            RagDocumentVersion.published_on,
            RagDocumentVersion.is_active,
            RagDocumentVersion.ingested_at,
        )
        .join(RagDocument, RagDocument.doc_id == RagDocumentVersion.doc_id)
        .order_by(RagDocumentVersion.ingested_at.desc())
    )
    return list(session.execute(query).all())


def activate_version(session: Session, version_id: int) -> tuple[RagDocumentVersion, str]:
    """Make one version active for its document, retiring the rest. For a rollback."""
    version = session.get(RagDocumentVersion, version_id)
    if version is None:
        raise VersionNotFound(version_id)

    session.execute(
        update(RagDocumentVersion)
        .where(RagDocumentVersion.doc_id == version.doc_id)
        .values(is_active=False)
    )
    version.is_active = True
    session.flush()

    doc = session.get(RagDocument, version.doc_id)
    return version, doc.title


def search(
    session: Session,
    *,
    product: str | None = None,
    angle: str | None = None,
    q: str | None = None,
    k: int = 5,
) -> list[Retrieved]:
    """What a draft would retrieve for a product and angle, or a raw probe query via q.

    q, when given, replaces the constructed query entirely rather than adding
    to it, so an admin can test the corpus against arbitrary text instead of
    only the angle-shaped queries generation itself builds. A raw probe returns
    the whole ranked tail, unfiltered, since seeing the weak matches is the
    point of probing; the product path mirrors a draft and so inherits the
    configured breadth and similarity floor.
    """
    if q is not None:
        sections = sections_for_product(product) if product else None
        return retrieve(session, q, sections=sections, k=k)
    if product is not None:
        return retrieve_product_facts(session, product=product, angle=angle)
    raise ValueError("either product or q is required")
