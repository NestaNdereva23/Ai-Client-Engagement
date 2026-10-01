from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.rag.guides import (
    GuideNotFound,
    GuideRejected,
    GuideVersion,
    add_guide,
    approve_guide_version,
    list_guide_versions,
)
from app.schemas.rag import (
    GuideIn,
    GuideOut,
    RagIngestOut,
    RagVersionOut,
    RetrievedChunkOut,
)
from app.services.rag import (
    VersionNotApproved,
    VersionNotFound,
    activate_version,
    ingest_uploaded_report,
)
from app.services.rag import list_versions as list_rag_versions
from app.services.rag import search as search_rag

router = APIRouter(prefix="/rag", tags=["rag"], dependencies=[Depends(get_current_reviewer_id)])


@router.post("/reports", response_model=RagIngestOut, status_code=201)
def upload_report(
    file: UploadFile,
    document_title: str | None = Form(None),
    document_source: str | None = Form(None),
    session: Session = Depends(get_session),
) -> RagIngestOut:
    content = file.file.read()
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
    try:
        version, document_title = activate_version(session, version_id)
    except VersionNotFound:
        raise HTTPException(status_code=404, detail="version not found") from None
    except VersionNotApproved:
        raise HTTPException(
            status_code=409, detail="a client guide must be approved before it goes live"
        ) from None
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
            doc_type=r.doc_type,
        )
        for r in results
    ]


def _guide_out(guide: GuideVersion) -> GuideOut:
    if guide.is_active:
        status = "live"
    elif guide.approved_at is not None:
        status = "retired"
    else:
        status = "waiting_for_approval"
    return GuideOut(
        doc_id=guide.doc_id,
        version_id=guide.version_id,
        version_no=guide.version_no,
        title=guide.title,
        topic=guide.topic,
        text=guide.text,
        status=status,
        approved_by=guide.approved_by,
        approved_at=guide.approved_at,
    )


@router.get("/guides", response_model=list[GuideOut])
def get_client_guides(session: Session = Depends(get_session)) -> list[GuideOut]:
    return [_guide_out(guide) for guide in list_guide_versions(session)]


@router.post("/guides", response_model=GuideOut, status_code=201)
def create_client_guide(
    body: GuideIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> GuideOut:
    try:
        guide = add_guide(
            session,
            title=body.title,
            topic=body.topic,
            text=body.text,
            created_by=reviewer_id,
        )
    except GuideRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _guide_out(guide)


@router.post("/guides/versions/{version_id}/approve", response_model=GuideOut)
def approve_client_guide(
    version_id: int,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> GuideOut:
    try:
        guide = approve_guide_version(session, version_id, approved_by=reviewer_id)
    except GuideNotFound:
        raise HTTPException(status_code=404, detail="guide version not found") from None
    session.commit()
    return _guide_out(guide)
