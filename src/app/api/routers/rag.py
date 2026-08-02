"""RAG admin console: upload the weekly report, manage versions, debug retrieval."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.rag import RagIngestOut, RagVersionOut, RetrievedChunkOut
from app.services.rag import VersionNotFound, activate_version, ingest_uploaded_report
from app.services.rag import list_versions as list_rag_versions
from app.services.rag import search as search_rag

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/reports", response_model=RagIngestOut, status_code=201)
async def upload_report(
    file: UploadFile,
    document_title: str | None = Form(None),
    document_source: str | None = Form(None),
    session: Session = Depends(get_session),
) -> RagIngestOut:
    """Upload the weekly report PDF; it becomes the new active version.

    document_title and document_source default to the one ongoing weekly
    series; override them only to start a separate report series.
    """
    content = await file.read()
    try:
        result = ingest_uploaded_report(
            session, content, document_title=document_title, document_source=document_source
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return RagIngestOut(
        doc_id=result.doc_id,
        version_id=result.version_id,
        version_no=result.version_no,
        chunks=result.chunks,
        created=result.created,
    )


@router.get("/versions", response_model=list[RagVersionOut])
def get_rag_versions(session: Session = Depends(get_session)) -> list[RagVersionOut]:
    """Version history across every ingested document, newest first."""
    rows = list_rag_versions(session)
    return [
        RagVersionOut(
            version_id=r.version_id,
            doc_id=r.doc_id,
            document_title=r.title,
            version_no=r.version_no,
            issue=r.source,
            published_on=r.published_on,
            is_active=r.is_active,
            ingested_at=r.ingested_at,
        )
        for r in rows
    ]


@router.post("/versions/{version_id}/activate", response_model=RagVersionOut)
def activate_rag_version(version_id: int, session: Session = Depends(get_session)) -> RagVersionOut:
    """Make one version active again, for rolling back a bad ingest."""
    try:
        version, document_title = activate_version(session, version_id)
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="version not found") from None
    session.commit()
    return RagVersionOut(
        version_id=version.version_id,
        doc_id=version.doc_id,
        document_title=document_title,
        version_no=version.version_no,
        issue=version.source,
        published_on=version.published_on,
        is_active=version.is_active,
        ingested_at=version.ingested_at,
    )


@router.get("/search", response_model=list[RetrievedChunkOut])
def search_rag_corpus(
    product: str | None = None,
    angle: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
) -> list[RetrievedChunkOut]:
    """Debug retrieval: what facts a draft would see for this product and angle,
    or whatever q matches when given.
    """
    try:
        results = search_rag(session, product=product, angle=angle, q=q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return [
        RetrievedChunkOut(
            chunk_id=r.chunk_id,
            text=r.text,
            metadata=r.metadata,
            score=r.score,
            version_id=r.version_id,
        )
        for r in results
    ]
