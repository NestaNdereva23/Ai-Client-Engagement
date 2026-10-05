from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.agent import AgentActionCatalog
from app.db.models.rag import DOC_TYPE_CLIENT_GUIDE, RagChunk, RagDocument, RagDocumentVersion
from app.privacy.scanners import OutboundLeak, scan_outbound
from app.rag.chunking import ReportChunk
from app.rag.embedding import Embedder, get_embedder
from app.rag.index import index_chunks
from app.rag.retrieve import Retrieved

GUIDE_MAX_CHARS = 1000
GUIDES_PER_DRAFT = 1
AUDIT_ENTITY = "rag_document_version"


class GuideRejected(ValueError):
    pass


class GuideNotFound(LookupError):
    pass


@dataclass(frozen=True)
class GuideVersion:
    doc_id: int
    version_id: int
    version_no: int
    title: str
    topic: str
    text: str
    action_codes: tuple[str, ...]
    is_active: bool
    approved_by: str | None
    approved_at: datetime | None


def _check_actions(session: Session, action_codes: Sequence[str]) -> None:
    known = set(session.scalars(select(AgentActionCatalog.action_code).distinct()))
    unknown = sorted(set(action_codes) - known)
    if unknown:
        raise GuideRejected(f"these are not actions the agent can take: {', '.join(unknown)}")


def _check(title: str, topic: str, text: str) -> None:
    if not (title and topic and text):
        raise GuideRejected("a client guide needs a title, a topic and some text")
    if len(text) > GUIDE_MAX_CHARS:
        raise GuideRejected(
            f"a client guide explains one idea in at most {GUIDE_MAX_CHARS} characters"
        )
    try:
        scan_outbound(text)
    except OutboundLeak:
        raise GuideRejected(
            "a client guide may not hold an email address or a phone or account number"
        ) from None


def _guide_document(session: Session, title: str) -> RagDocument:
    document = session.scalar(
        select(RagDocument).where(
            RagDocument.doc_type == DOC_TYPE_CLIENT_GUIDE,
            func.lower(RagDocument.title) == title.lower(),
        )
    )
    if document is None:
        document = RagDocument(title=title, doc_type=DOC_TYPE_CLIENT_GUIDE)
        session.add(document)
        session.flush()
    return document


def _next_version_no(session: Session, doc_id: int) -> int:
    latest = session.scalar(
        select(func.max(RagDocumentVersion.version_no)).where(RagDocumentVersion.doc_id == doc_id)
    )
    return (latest or 0) + 1


def _guide_versions(session: Session, version_id: int | None = None) -> list[GuideVersion]:
    stmt = (
        select(RagDocumentVersion, RagDocument.title, RagChunk.text, RagChunk.chunk_metadata)
        .join(RagDocument, RagDocument.doc_id == RagDocumentVersion.doc_id)
        .join(RagChunk, RagChunk.version_id == RagDocumentVersion.version_id)
        .where(RagDocument.doc_type == DOC_TYPE_CLIENT_GUIDE, RagChunk.ordinal == 0)
        .order_by(RagDocumentVersion.ingested_at.desc(), RagDocumentVersion.version_id.desc())
    )
    if version_id is not None:
        stmt = stmt.where(RagDocumentVersion.version_id == version_id)
    return [
        GuideVersion(
            doc_id=version.doc_id,
            version_id=version.version_id,
            version_no=version.version_no,
            title=title,
            topic=(metadata or {}).get("topic", ""),
            text=text,
            action_codes=tuple((metadata or {}).get("actions", ())),
            is_active=version.is_active,
            approved_by=version.approved_by,
            approved_at=version.approved_at,
        )
        for version, title, text, metadata in session.execute(stmt).all()
    ]


def list_guide_versions(session: Session) -> list[GuideVersion]:
    return _guide_versions(session)


def add_guide(
    session: Session,
    *,
    title: str,
    topic: str,
    text: str,
    created_by: str | None,
    action_codes: Sequence[str] = (),
    embedder: Embedder | None = None,
) -> GuideVersion:
    title, topic, text = title.strip(), topic.strip(), text.strip()
    actions = sorted(set(action_codes))
    _check(title, topic, text)
    _check_actions(session, actions)
    document = _guide_document(session, title)
    version = RagDocumentVersion(
        doc_id=document.doc_id,
        version_no=_next_version_no(session, document.doc_id),
        is_active=False,
    )
    session.add(version)
    session.flush()
    record_audit(
        session,
        entity_type=AUDIT_ENTITY,
        action="create_guide",
        entity_id=str(version.version_id),
        actor_id=created_by,
        detail={"doc_id": document.doc_id, "title": title, "topic": topic, "actions": actions},
    )
    version_id = version.version_id
    index_chunks(
        session,
        version_id,
        [ReportChunk(ordinal=0, text=text, metadata={"topic": topic, "actions": actions})],
        embedder=embedder,
    )
    return _guide_versions(session, version_id)[0]


def approve_guide_version(session: Session, version_id: int, *, approved_by: str) -> GuideVersion:
    version = session.get(RagDocumentVersion, version_id)
    document = None if version is None else session.get(RagDocument, version.doc_id)
    if version is None or document is None or document.doc_type != DOC_TYPE_CLIENT_GUIDE:
        raise GuideNotFound(version_id)

    if version.approved_at is None:
        version.approved_by = approved_by
        version.approved_at = datetime.now(UTC)
        record_audit(
            session,
            entity_type=AUDIT_ENTITY,
            action="approve_guide",
            entity_id=str(version_id),
            actor_id=approved_by,
            detail={"doc_id": document.doc_id, "title": document.title},
        )
    session.execute(
        update(RagDocumentVersion)
        .where(RagDocumentVersion.doc_id == version.doc_id)
        .values(is_active=False)
    )
    session.execute(
        update(RagDocumentVersion)
        .where(RagDocumentVersion.version_id == version_id)
        .values(is_active=True)
    )
    session.flush()
    session.expire_all()
    return _guide_versions(session, version_id)[0]


def find_guides(
    session: Session, action_code: str, query: str, *, embedder: Embedder | None = None
) -> list[Retrieved]:
    embedder = embedder or get_embedder()
    vector = embedder.embed([query])[0]
    matching = (
        select(
            RagChunk.chunk_id,
            RagChunk.text,
            RagChunk.chunk_metadata.label("chunk_metadata"),
            RagChunk.version_id,
            RagChunk.embedding,
        )
        .join(RagDocumentVersion, RagChunk.version_id == RagDocumentVersion.version_id)
        .join(RagDocument, RagDocumentVersion.doc_id == RagDocument.doc_id)
        .where(
            RagChunk.embedding.isnot(None),
            RagDocumentVersion.is_active.is_(True),
            RagDocument.doc_type == DOC_TYPE_CLIENT_GUIDE,
            RagChunk.chunk_metadata.contains({"actions": [action_code]}),
        )
        .offset(0)
        .subquery("matching_guides")
    )
    distance = matching.c.embedding.cosine_distance(vector)
    rows = session.execute(
        select(
            matching.c.chunk_id,
            matching.c.text,
            matching.c.chunk_metadata,
            matching.c.version_id,
            distance.label("distance"),
        )
        .order_by(distance)
        .limit(GUIDES_PER_DRAFT)
    ).all()
    return [
        Retrieved(
            chunk_id=row.chunk_id,
            text=row.text,
            metadata=row.chunk_metadata or {},
            score=1.0 - float(row.distance),
            version_id=row.version_id,
            doc_type=DOC_TYPE_CLIENT_GUIDE,
        )
        for row in rows
    ]
