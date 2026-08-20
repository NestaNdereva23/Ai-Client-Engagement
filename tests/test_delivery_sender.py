"""Tests for delivery.sender.build_email_sender: the one real SenderFn,
addressing an approved outreach_message and handing it to a Mailer.

No test here opens a real socket: the Mailer is always either NullMailer or
a small fake standing in for a configured provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete

from app.campaigns.touch import SendBlocked
from app.db.models.models import PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import restricted_session
from app.delivery.mailer import EmailMessage, NullMailer
from app.delivery.mailer import SendResult as MailerSendResult
from app.delivery.sender import build_email_sender

CLIENT_ID = 9977801


def a_message(**overrides) -> OutreachMessage:
    defaults = dict(
        message_id="msg-1",
        campaign_id=1,
        generation_run_id="run-1",
        client_id=CLIENT_ID,
        ai_draft_content={"subject": "{{first_name}}, draft subject", "body": "draft body"},
        personalized_content={"subject": "Real subject", "body": "Real body"},
    )
    defaults.update(overrides)
    return OutreachMessage(**defaults)


@dataclass
class FakeMailer:
    """Stands in for a configured provider: records what it sent and reports success."""

    sent_messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> MailerSendResult:
        self.sent_messages.append(message)
        return MailerSendResult(
            sent=True, sender="ace@example.com", recipient=message.to, subject=message.subject
        )


@pytest.fixture
def client_with_contact(db: None):
    """One client with a contact_email on file in pii_vault."""
    with restricted_session() as session:
        session.add(PiiVault(client_id=CLIENT_ID, contact_email="client@example.com"))
        session.commit()
    yield
    with restricted_session() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.commit()


def test_sends_the_personalized_content_to_the_vault_contact(client_with_contact: None):
    mailer = FakeMailer()
    sender = build_email_sender(mailer)

    result = sender(a_message())

    assert result.delivery_status == "sent"
    assert len(mailer.sent_messages) == 1
    sent = mailer.sent_messages[0]
    assert sent.to == "client@example.com"
    assert sent.subject == "Real subject"
    assert sent.text_body == "Real body"


def test_reports_recorded_when_the_mailer_is_the_recording_no_op(client_with_contact: None):
    """A no-op mailer (no SMTP host configured) still reports the touch as
    handled -- the same "recorded, not sent" status the digest email uses.
    """
    sender = build_email_sender(NullMailer(sender="ace@example.com"))

    result = sender(a_message())

    assert result.delivery_status == "recorded"


def test_blocks_a_message_with_no_personalized_content(client_with_contact: None):
    """ai_draft_content still carries placeholders like {{first_name}}; a
    message that never got personalized must never reach the mailer.
    """
    sender = build_email_sender(FakeMailer())

    with pytest.raises(SendBlocked, match="no_personalized_content"):
        sender(a_message(personalized_content=None))


def test_blocks_a_client_with_no_contact_email_on_file(db: None):
    sender = build_email_sender(FakeMailer())

    with pytest.raises(SendBlocked, match="no_deliverable_contact"):
        sender(a_message(client_id=CLIENT_ID))
