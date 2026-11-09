"""Claims a campaign's due messages and gets them delivered.

The campaign's normal send pipeline runs unchanged either way: approval, the
send-time stop-condition recheck, the vault read, the audit trail and
advancing the enrollment. Only the last step has two modes.

Normally, each message is captured as a ready-to-send Delivery and returned;
the Ticketing app queues it and sends it later. Nothing is reported back, so
a claimed message counts as sent as soon as Ticketing accepts the batch. Each
delivery carries its touch id and step number, so Ticketing can tell one touch
to a client from the next.

When settings.dispatch_direct_send is on, ACE sends each message itself
during this same call, through Ticketing's plain send-email/send-sms
endpoints, and returns no deliveries; there is nothing left for Ticketing to
queue. This exists as a fallback for when Ticketing's queue worker isn't
draining jobs, so a launch doesn't just sit there unsent. It's a blocking
call for as many messages as it sends, which is why the batch size is capped
separately from the caller's own limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.guardrails import sms_part_count
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.touch import SendOutcome
from app.config import Settings, get_settings
from app.db.models.campaigns import TouchLog
from app.delivery.mailer import EmailMessage, Mailer, SendResult
from app.delivery.sender import build_email_sender
from app.delivery.sms_gateway import SmsGateway, SmsMessage, SmsSendResult
from app.delivery.sms_sender import build_sms_sender
from app.services.campaigns import send_campaign


@dataclass(frozen=True)
class Delivery:
    channel: str
    to: str
    body: str
    subject: str | None = None
    cc: tuple[str, ...] = ()
    message_id: str | None = None
    touch_id: int | None = None
    step_no: int | None = None


@dataclass
class CapturingMailer:
    deliveries: list[Delivery] = field(default_factory=list)

    def send(self, message: EmailMessage) -> SendResult:
        self.deliveries.append(
            Delivery(
                channel="email",
                to=message.to,
                subject=message.subject,
                body=message.text_body,
                cc=message.cc,
                message_id=message.reference,
            )
        )
        return SendResult(
            sent=True, sender="ticketing", recipient=message.to, subject=message.subject
        )

    def close(self) -> None:
        pass


@dataclass
class CapturingSmsGateway:
    deliveries: list[Delivery] = field(default_factory=list)

    def send(self, message: SmsMessage) -> SmsSendResult:
        self.deliveries.append(
            Delivery(channel="sms", to=message.to, body=message.body, message_id=message.reference)
        )
        return SmsSendResult(
            sent=True,
            sender="ticketing",
            recipient=message.to,
            body=message.body,
            parts=sms_part_count(message.body),
        )

    def close(self) -> None:
        pass


def _with_touches(session: Session, deliveries: list[Delivery]) -> list[Delivery]:
    # A delivery with no touch row still goes out: ACE has already claimed it,
    # and Ticketing falls back to the recipient when the touch id is missing.
    if not deliveries:
        return deliveries
    message_ids = [d.message_id for d in deliveries if d.message_id]
    touches = {
        message_id: (touch_id, step_no)
        for message_id, touch_id, step_no in session.execute(
            select(TouchLog.message_id, TouchLog.touch_id, TouchLog.step_no).where(
                TouchLog.message_id.in_(message_ids)
            )
        )
    }
    resolved = []
    for delivery in deliveries:
        touch_id, step_no = touches.get(delivery.message_id, (None, None))
        resolved.append(replace(delivery, touch_id=touch_id, step_no=step_no))
    return resolved


def dispatch_campaign(
    session: Session,
    campaign_id: int,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
    sms_gateway: SmsGateway | None = None,
) -> tuple[list[SendOutcome], list[Delivery]]:
    settings = settings or get_settings()
    if settings.dispatch_direct_send:
        limit = min(limit, settings.dispatch_direct_send_max_batch)
        email_sender = build_email_sender(mailer, settings=settings)
        sms_sender = build_sms_sender(sms_gateway, settings=settings)
        outcomes = send_campaign(
            session,
            campaign_id,
            sender=email_sender,
            senders={"email": email_sender, "sms": sms_sender},
            limit=limit,
        )
        return outcomes, []

    deliveries: list[Delivery] = []
    capturing_mailer = CapturingMailer(deliveries)
    capturing_gateway = CapturingSmsGateway(deliveries)
    email_sender = build_email_sender(capturing_mailer, settings=settings)
    outcomes = send_campaign(
        session,
        campaign_id,
        sender=email_sender,
        senders={
            "email": email_sender,
            "sms": build_sms_sender(capturing_gateway, settings=settings),
        },
        limit=limit,
    )
    return outcomes, _with_touches(session, deliveries)
