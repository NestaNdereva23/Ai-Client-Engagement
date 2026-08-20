"""Turns an approved outreach_message into an actual send.

campaigns/touch.py owns everything about *whether* a touch may go out: the
approval check, the send-time stop-condition recheck, the audit trail, and
advancing the enrollment once it does. It stays free of any provider
dependency on purpose, so it takes a plain SenderFn callable instead. This
module is that callable's one real implementation: address the message and
hand it to whatever Mailer app.delivery.mailer.get_mailer() resolves to --
Mailpit in development, a recording no-op wherever SMTP is not configured,
and a real provider once one is.

personalized_content is required. ai_draft_content is what the model saw --
it still carries unresolved placeholders like {{first_name}} -- and must
never be what reaches a client. A message with no personalized_content
reaching this sender is a bug upstream (create_outreach_message always sets
it), not something to paper over here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from app.audit.log import record_audit
from app.campaigns.touch import SendBlocked, SenderFn, SendResult
from app.config import Settings, get_settings
from app.db.models.models import PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import restricted_session
from app.delivery.mailer import EmailMessage, Mailer, get_mailer

logger = structlog.get_logger(__name__)


def _contact_email(client_id: int) -> str | None:
    """This client's send-to address, read once under the restricted role."""
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
        return vault.contact_email if vault else None


def build_email_sender(
    mailer: Mailer | None = None, *, settings: Settings | None = None
) -> SenderFn:
    """A SenderFn that hands an approved message to the configured Mailer.

    mailer defaults to get_mailer(settings): the recording no-op with no
    SMTP host configured, the same fallback every other mail path in the
    app already uses, so an unconfigured environment stays quiet rather
    than raising mid-send.
    """
    mailer = mailer if mailer is not None else get_mailer(settings or get_settings())

    def send(message: OutreachMessage) -> SendResult:
        content = message.personalized_content
        if not content:
            raise SendBlocked("no_personalized_content")

        to = _contact_email(message.client_id)
        if not to:
            raise SendBlocked("no_deliverable_contact")

        result = mailer.send(
            EmailMessage(to=to, subject=content["subject"], text_body=content["body"])
        )
        status = "sent" if result.sent else "recorded"
        logger.info("outreach_message.send", message_id=message.message_id, status=status)
        return SendResult(delivery_status=status, sent_at=datetime.now(UTC))

    return send
