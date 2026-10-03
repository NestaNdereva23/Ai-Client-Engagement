from __future__ import annotations

from sqlalchemy import func, select

from app.audit.log import record_audit
from app.campaigns.touch import SendBlocked
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.outreach import OutreachMessage
from app.db.session import SessionLocal, restricted_session
from app.delivery.test_list import active_test_contacts


def _sends_so_far(campaign_id: int, channel: str) -> int:
    with SessionLocal() as session:
        return session.scalar(
            select(func.count())
            .select_from(TouchLog)
            .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
            .join(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
            .where(
                Enrollment.campaign_id == campaign_id,
                OutreachMessage.channel == channel,
                TouchLog.sent_at.is_not(None),
            )
        )


def pick_test_recipient(client_id: int, channel: str, campaign_id: int) -> str:
    sent = _sends_so_far(campaign_id, channel)
    with restricted_session() as session:
        contacts = active_test_contacts(session, channel)
        if not contacts:
            raise SendBlocked("no_test_recipient")
        # Testers take turns, so everyone gets one before anyone gets a second. Each campaign
        # starts one tester further along, so small campaigns do not always reach the same ones.
        to = contacts[(campaign_id + sent) % len(contacts)]
        record_audit(
            session,
            entity_type="test_recipient",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "test_send_redirect", "channel": channel},
        )
        session.commit()
        return to


def ensure_test_recipient(to: str, channel: str) -> None:
    with restricted_session() as session:
        allowed = active_test_contacts(session, channel)
    if to not in allowed:
        raise SendBlocked("recipient_not_on_test_list")
