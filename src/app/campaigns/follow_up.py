from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.campaigns import TouchLog
from app.db.models.outreach import OutreachMessage
from app.db.models.rag import DOC_TYPE_REPORT, RagChunk, RagDocument, RagDocumentVersion
from app.rag.retrieve import sections_for_product

PERCENT = r"[0-9]+(\.[0-9]+)?\s*%"
RATE_LIMIT = 2
BODY_LIMIT = 600

RATE_GUIDANCE = (
    "Keep the same angle and the same purpose, and keep the email personal to this client "
    "within what the angle allows. Give the client a new reason to act: use the current published "
    "rate or yield from the facts you may cite, quoted exactly as written, as a published "
    "figure and never as a promise or a guarantee. For the rate only, this overrides any angle "
    "guidance that says not to use retrieved product or market facts; every other limit still "
    "applies. If those facts hold no rate, do not mention one."
)

REPEAT_GUIDANCE = (
    "Do not reuse the subject, the opening line or the call to action of the earlier emails "
    "below, and do not repeat their wording."
)


def follow_up_rate_chunks(session: Session, product: str) -> list[RagChunk]:
    sections = sections_for_product(product)
    if not sections or not get_settings().rag_enabled:
        return []
    return list(
        session.scalars(
            select(RagChunk)
            .join(RagDocumentVersion, RagChunk.version_id == RagDocumentVersion.version_id)
            .join(RagDocument, RagDocumentVersion.doc_id == RagDocument.doc_id)
            .where(
                RagDocumentVersion.is_active.is_(True),
                RagDocument.doc_type == DOC_TYPE_REPORT,
                RagChunk.chunk_metadata["section"].astext.in_(sections),
                RagChunk.text.ilike(f"%{product}%"),
                RagChunk.text.op("~")(PERCENT),
            )
            .order_by(RagChunk.chunk_id)
            .limit(RATE_LIMIT)
        )
    )


def follow_up_note(session: Session, enrollment_id: int, step_no: int) -> str | None:
    if step_no < 2:
        return None

    rows = session.execute(
        select(TouchLog.step_no, TouchLog.sent_at, OutreachMessage.ai_draft_content)
        .join(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
        .where(TouchLog.enrollment_id == enrollment_id, TouchLog.step_no < step_no)
        .order_by(TouchLog.step_no)
    ).all()

    intro = f"This is follow-up email {step_no}."
    sent = [row.sent_at for row in rows if row.sent_at is not None]
    if sent:
        intro += f" The previous email went out {(datetime.now(UTC) - max(sent)).days} days ago."

    parts = [intro, RATE_GUIDANCE]
    if rows:
        parts.append(REPEAT_GUIDANCE)
        parts.extend(
            f"Email {row.step_no}:\n"
            f"Subject: {row.ai_draft_content.get('subject', '')}\n"
            f"Body: {str(row.ai_draft_content.get('body', ''))[:BODY_LIMIT]}"
            for row in rows
        )
    return "\n\n".join(parts)
