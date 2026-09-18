from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.touch import SendBlocked, SenderFn, SendResult
from app.config import Settings, get_settings
from app.db.models.models import PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import restricted_session
from app.delivery.sms_gateway import SmsGateway, SmsMessage, SmsSendResult, get_sms_gateway
from app.delivery.test_recipients import ensure_test_recipient, pick_test_recipient

logger = structlog.get_logger(__name__)


def _contact_phone(client_id: int) -> str | None:
    with restricted_session() as session:
        vault = session.get(PiiVault, client_id)
        record_audit(
            session,
            entity_type="pii_vault",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "outreach_send"},
        )
        session.commit()
        return vault.contact_phone if vault else None


def build_sms_sender(
    gateway: SmsGateway | None = None, *, settings: Settings | None = None
) -> SenderFn:
    settings = settings or get_settings()
    gateway = gateway if gateway is not None else get_sms_gateway(settings)
    test_mode = settings.delivery_mode == "test"

    def send(message: OutreachMessage) -> SendResult:
        content = message.personalized_content
        if not content:
            raise SendBlocked("no_personalized_content")

        body = content["body"]
        if test_mode:
            to = pick_test_recipient(message.client_id, "sms")
            body = f"{settings.test_subject_prefix}{body} (client {message.client_id})"
        else:
            to = _contact_phone(message.client_id)
        if not to:
            raise SendBlocked("no_deliverable_contact")
        # Checked again right before sending, whatever picked the number.
        if test_mode:
            ensure_test_recipient(to, "sms")

        result = gateway.send(SmsMessage(to=to, body=body))
        status = "sent" if result.sent else "recorded"
        logger.info(
            "outreach_message.send",
            message_id=message.message_id,
            status=status,
            parts=result.parts,
        )
        return SendResult(
            delivery_status=status,
            sent_at=datetime.now(UTC),
            provider_status=result.provider_status,
            parts=result.parts,
            cost=result.cost,
            recipient=to if test_mode else None,
        )

    send.gateway = gateway
    return send


def send_test_sms(
    session: Session,
    *,
    to: str,
    body: str,
    gateway: SmsGateway | None = None,
    settings: Settings | None = None,
) -> SmsSendResult:
    gateway = gateway if gateway is not None else get_sms_gateway(settings or get_settings())
    result = gateway.send(SmsMessage(to=to, body=body))
    record_audit(
        session,
        entity_type="sms_test_send",
        action="send",
        entity_id=to,
        detail={
            "sent": result.sent,
            "parts": result.parts,
            "provider_status": result.provider_status,
        },
    )
    session.commit()
    return result
