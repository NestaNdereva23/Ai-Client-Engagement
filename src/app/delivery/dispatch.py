"""Claims a campaign's due messages for the Ticketing app to deliver.

The campaign's normal send pipeline runs unchanged: approval, the send-time
stop-condition recheck, the vault read, the audit trail and advancing the
enrollment. Only the last step differs. Instead of handing each message to a
provider, it is captured as a ready-to-send Delivery and returned, and the
Ticketing app sends it. Nothing is reported back, so a claimed message
counts as sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.guardrails import sms_part_count
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.touch import SendOutcome
from app.config import Settings, get_settings
from app.delivery.mailer import EmailMessage, SendResult
from app.delivery.sender import build_email_sender
from app.delivery.sms_gateway import SmsMessage, SmsSendResult
from app.delivery.sms_sender import build_sms_sender
from app.services.campaigns import send_campaign


@dataclass(frozen=True)
class Delivery:
    channel: str
    to: str
    body: str
    subject: str | None = None


@dataclass
class CapturingMailer:
    deliveries: list[Delivery] = field(default_factory=list)

    def send(self, message: EmailMessage) -> SendResult:
        self.deliveries.append(
            Delivery(
                channel="email", to=message.to, subject=message.subject, body=message.text_body
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
        self.deliveries.append(Delivery(channel="sms", to=message.to, body=message.body))
        return SmsSendResult(
            sent=True,
            sender="ticketing",
            recipient=message.to,
            body=message.body,
            parts=sms_part_count(message.body),
        )

    def close(self) -> None:
        pass


def dispatch_campaign(
    session: Session,
    campaign_id: int,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    settings: Settings | None = None,
) -> tuple[list[SendOutcome], list[Delivery]]:
    settings = settings or get_settings()
    deliveries: list[Delivery] = []
    mailer = CapturingMailer(deliveries)
    gateway = CapturingSmsGateway(deliveries)
    email_sender = build_email_sender(mailer, settings=settings)
    outcomes = send_campaign(
        session,
        campaign_id,
        sender=email_sender,
        senders={"email": email_sender, "sms": build_sms_sender(gateway, settings=settings)},
        limit=limit,
    )
    return outcomes, deliveries
