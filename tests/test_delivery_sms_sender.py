from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, select

from app.campaigns.touch import SendBlocked
from app.db.models.audit import AuditLog
from app.db.models.models import PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import SessionLocal, restricted_session
from app.delivery.sms_gateway import RecordingSmsGateway, SmsMessage
from app.delivery.sms_gateway import SmsSendResult as GatewaySendResult
from app.delivery.sms_sender import build_sms_sender, send_test_sms

CLIENT_ID = 9977802


def a_message(**overrides) -> OutreachMessage:
    defaults = dict(
        message_id="sms-msg-1",
        campaign_id=1,
        generation_run_id="run-1",
        client_id=CLIENT_ID,
        channel="sms",
        ai_draft_content={"body": "Hi {{first_name}}, {{fund_name}} draft body."},
        personalized_content={"body": "Hi Jane, Money Market Fund is open again."},
    )
    defaults.update(overrides)
    return OutreachMessage(**defaults)


@dataclass
class FakeGateway:
    sent_messages: list[SmsMessage] = field(default_factory=list)

    def send(self, message: SmsMessage) -> GatewaySendResult:
        self.sent_messages.append(message)
        return GatewaySendResult(
            sent=True,
            sender="ACE",
            recipient=message.to,
            body=message.body,
            provider_status="Success",
            parts=1,
            cost=0.8,
        )


@pytest.fixture
def client_with_contact(db: None):
    with restricted_session() as session:
        session.add(PiiVault(client_id=CLIENT_ID, contact_phone="+254712345678"))
        session.commit()
    yield
    with restricted_session() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.commit()


def test_sends_the_personalized_body_to_the_vault_contact_phone(client_with_contact: None):
    gateway = FakeGateway()
    sender = build_sms_sender(gateway)

    result = sender(a_message())

    assert result.delivery_status == "sent"
    assert result.provider_status == "Success"
    assert result.parts == 1
    assert result.cost == 0.8
    assert len(gateway.sent_messages) == 1
    sent = gateway.sent_messages[0]
    assert sent.to == "+254712345678"
    assert sent.body == "Hi Jane, Money Market Fund is open again."


def test_reports_recorded_when_the_gateway_is_the_recording_no_op(client_with_contact: None):
    sender = build_sms_sender(RecordingSmsGateway(sender="ACE"))

    result = sender(a_message())

    assert result.delivery_status == "recorded"


def test_blocks_a_message_with_no_personalized_content(client_with_contact: None):
    sender = build_sms_sender(FakeGateway())

    with pytest.raises(SendBlocked, match="no_personalized_content"):
        sender(a_message(personalized_content=None))


def test_blocks_a_client_with_no_contact_phone_on_file(db: None):
    sender = build_sms_sender(FakeGateway())

    with pytest.raises(SendBlocked, match="no_deliverable_contact"):
        sender(a_message(client_id=CLIENT_ID))


def test_send_test_sms_sends_and_audits_apart_from_any_campaign(db: None):
    gateway = FakeGateway()
    with SessionLocal() as session:
        result = send_test_sms(session, to="+254700000001", body="Test message", gateway=gateway)

        assert result.sent is True
        assert gateway.sent_messages[0].to == "+254700000001"

        row = session.scalar(
            select(AuditLog)
            .where(AuditLog.entity_type == "sms_test_send", AuditLog.entity_id == "+254700000001")
            .order_by(AuditLog.log_id.desc())
        )
        assert row is not None
        assert row.detail["sent"] is True

        session.execute(delete(AuditLog).where(AuditLog.log_id == row.log_id))
        session.commit()
