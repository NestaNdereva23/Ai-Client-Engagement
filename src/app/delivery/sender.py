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

import re
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, union

from app.audit.log import record_audit
from app.campaigns.touch import SendBlocked, SenderFn, SendResult
from app.config import Settings, get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.models import ClientFund, PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import SessionLocal, restricted_session
from app.delivery.mailer import EmailMessage, Mailer, get_mailer
from app.delivery.test_recipients import ensure_test_recipient, pick_test_recipient

logger = structlog.get_logger(__name__)

_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def _advisor_emails(client_id: int) -> tuple[str, ...]:
    """The account managers to copy on this client's email, each address once.

    A client can be in the inactive book, the active book, or both. An address that is
    not shaped like an email is left out, so one bad value cannot stop the send.
    """
    inactive = select(ClientFund.fa_email).where(
        ClientFund.client_id == client_id, ClientFund.fa_email.is_not(None)
    )
    active = select(ActiveClientFund.fa_email).where(
        ActiveClientFund.client_id == client_id, ActiveClientFund.fa_email.is_not(None)
    )
    with SessionLocal() as session:
        addresses = session.scalars(union(inactive, active)).all()
    return tuple(sorted(address for address in addresses if _EMAIL_SHAPE.match(address)))


def build_email_sender(
    mailer: Mailer | None = None, *, settings: Settings | None = None
) -> SenderFn:
    """A SenderFn that hands an approved message to the configured Mailer.

    mailer defaults to get_mailer(settings): the recording no-op with no
    SMTP host configured, the same fallback every other mail path in the
    app already uses, so an unconfigured environment stays quiet rather
    than raising mid-send.

    The chosen Mailer is exposed as send.mailer, so a caller sending a
    whole batch through this SenderFn (send_campaign, for one) can close()
    it once the batch is done instead of leaving a connection open.
    """
    settings = settings or get_settings()
    mailer = mailer if mailer is not None else get_mailer(settings)
    test_mode = settings.delivery_mode == "test"

    def send(message: OutreachMessage) -> SendResult:
        content = message.personalized_content
        if not content:
            raise SendBlocked("no_personalized_content")

        subject = content["subject"]
        body = content["body"]
        if test_mode:
            to = pick_test_recipient(message.client_id, "email", message.campaign_id)
            subject = f"{settings.test_subject_prefix.strip()} {subject}"
            body = (
                f"{body}\n\n--\nTest send for client {message.client_id}, "
                f"campaign {message.campaign_id}."
            )
        else:
            to = _contact_email(message.client_id)
        if not to:
            raise SendBlocked("no_deliverable_contact")
        # Checked again right before sending, whatever picked the address.
        if test_mode:
            ensure_test_recipient(to, "email")

        # A test send goes only to the test list, so no real account manager is copied.
        cc = () if test_mode else _advisor_emails(message.client_id)
        result = mailer.send(EmailMessage(to=to, subject=subject, text_body=body, cc=cc))
        status = "sent" if result.sent else "recorded"
        logger.info("outreach_message.send", message_id=message.message_id, status=status)
        return SendResult(
            delivery_status=status,
            sent_at=datetime.now(UTC),
            recipient=to if test_mode else None,
        )

    send.mailer = mailer
    return send
