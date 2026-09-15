from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.delivery.sms_gateway import RecordingSmsGateway
from app.main import app

client = TestClient(app)

TEST_SEND = "/api/v1/delivery/sms/test"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)


@pytest.fixture
def recording_gateway(monkeypatch) -> RecordingSmsGateway:
    gateway = RecordingSmsGateway(sender="ACE")
    from app.delivery import sms_sender as sms_sender_module

    monkeypatch.setattr(sms_sender_module, "get_sms_gateway", lambda *args, **kwargs: gateway)
    return gateway


def test_test_send_records_instead_of_sending_without_a_provider_configured(
    db: None, recording_gateway: RecordingSmsGateway
) -> None:
    response = client.post(
        TEST_SEND, json={"to": "+254700000002", "body": "Hello, this is a test."}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sent"] is False
    assert body["recipient"] == "+254700000002"
    assert [m.body for m in recording_gateway.sent_messages] == ["Hello, this is a test."]

    with SessionLocal() as session:
        row = session.scalar(
            select(AuditLog)
            .where(AuditLog.entity_type == "sms_test_send", AuditLog.entity_id == "+254700000002")
            .order_by(AuditLog.log_id.desc())
        )
        assert row is not None
        session.execute(delete(AuditLog).where(AuditLog.log_id == row.log_id))
        session.commit()


def test_test_send_requires_authentication(db: None, configured_reviewers) -> None:
    unauthenticated = TestClient(app)
    response = unauthenticated.post(TEST_SEND, json={"to": "+254700000003", "body": "hi"})
    assert response.status_code == 401
