from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.campaigns.nurture_bridge as nurture_bridge_mod
from app.campaigns.nurture_bridge import (
    AUTO_CHECKIN_ANGLE,
    AUTO_CHECKIN_CAMPAIGN_TYPE,
    enroll_auto_checkin_clients,
)
from app.campaigns.scheduler import select_due_enrollments
from app.campaigns.touch import run_due_enrollments, send_touch
from app.config import Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures, Clients, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.delivery import sender as sender_module
from app.delivery.mailer import NullMailer
from app.llmops.versions import persist_generation_run
from app.main import app

api_client = TestClient(app)
CAMPAIGNS = "/api/v1/campaigns"
REVIEWS = "/api/v1/reviews"

FUND_ID = 921
CLIENT_ID = 92101
CLIENT_ID_2 = 92102
COLLIDING_CLIENT_ID = 92103


def _fund_row(client_id: int, unit_fund_id: int, balance: float) -> ActiveClientFund:
    return ActiveClientFund(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        client_code=f"C{client_id}",
        balance=balance,
        n_deposits=3,
        n_withdrawals=0,
        last_deposit_date=date(2026, 1, 1),
    )


@pytest.fixture
def campaign_seed(db: None):
    with SessionLocal() as session:
        existing = session.scalar(
            select(Campaign).where(Campaign.campaign_type == AUTO_CHECKIN_CAMPAIGN_TYPE)
        )
        if existing is not None:
            yield existing.campaign_id
            return
        pytest.skip("auto_checkin_nurture campaign not seeded; run alembic upgrade head")


def _cleanup_client(session, client_id: int, campaign_id: int) -> None:
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(
            Enrollment.client_id == client_id, Enrollment.campaign_id == campaign_id
        )
    ).all()
    if enrollment_ids:
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
    session.execute(
        delete(Enrollment).where(
            Enrollment.client_id == client_id, Enrollment.campaign_id == campaign_id
        )
    )
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id == client_id)
    ).all()
    if message_ids:
        session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
    run_ids = session.scalars(
        select(GenerationRun.run_id).where(
            GenerationRun.run_id.in_(
                select(OutreachMessage.generation_run_id).where(
                    OutreachMessage.client_id == client_id
                )
            )
        )
    ).all()
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id == client_id))
    if run_ids:
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "enrollment",
            AuditLog.action.in_(("auto_checkin_sync", "enroll_cohort")),
            AuditLog.entity_id == str(campaign_id),
        )
    )
    session.execute(
        delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == client_id)
    )
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == client_id))
    session.execute(delete(Clients).where(Clients.client_id == client_id))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == client_id))
    session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))


@pytest.fixture
def active_client(campaign_seed: int):
    with SessionLocal() as session:
        session.add(_fund_row(CLIENT_ID, FUND_ID, balance=5_000.0))
        session.add(
            PiiVault(
                client_id=CLIENT_ID,
                client_name="Auto Checkin Client",
                contact_email="auto.checkin@example.com",
            )
        )
        session.commit()

    yield CLIENT_ID, campaign_seed

    with SessionLocal() as session:
        _cleanup_client(session, CLIENT_ID, campaign_seed)
        session.commit()


def test_enroll_auto_checkin_clients_creates_bridge_rows_and_enrolls(active_client) -> None:
    client_id, campaign_id = active_client
    with SessionLocal() as session:
        bridged = enroll_auto_checkin_clients(session, [client_id])
        session.commit()

        assert bridged == [client_id]

        client_row = session.get(Clients, client_id)
        features = session.get(ClientFeatures, client_id)
        indicator = session.get(ClientMessageIndicators, client_id)
        enrollment = session.scalar(
            select(Enrollment).where(
                Enrollment.client_id == client_id, Enrollment.campaign_id == campaign_id
            )
        )
        sync_audit = session.scalar(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "enrollment",
                AuditLog.action == "auto_checkin_sync",
                AuditLog.entity_id == str(campaign_id),
            )
            .order_by(AuditLog.log_id.desc())
            .limit(1)
        )

    assert client_row is not None
    assert client_row.balance == 5_000.0
    assert features is not None
    assert features.active_book_auto_checkin is True
    assert indicator is not None
    assert indicator.message_angle == AUTO_CHECKIN_ANGLE
    assert enrollment is not None
    assert enrollment.is_primary_contact_row is True
    assert sync_audit is not None
    assert sync_audit.detail["client_ids"] == [client_id]


def test_enroll_auto_checkin_clients_is_idempotent(active_client) -> None:
    client_id, campaign_id = active_client
    with SessionLocal() as session:
        enroll_auto_checkin_clients(session, [client_id])
        session.commit()
        enroll_auto_checkin_clients(session, [client_id])
        session.commit()

        enrollments = session.scalars(
            select(Enrollment).where(
                Enrollment.client_id == client_id, Enrollment.campaign_id == campaign_id
            )
        ).all()

    assert len(enrollments) == 1


def test_client_with_no_active_fund_rows_is_skipped(campaign_seed: int) -> None:
    with SessionLocal() as session:
        bridged = enroll_auto_checkin_clients(session, [999999])
        session.commit()

    assert bridged == []


def test_existing_clients_row_is_never_overwritten(campaign_seed: int) -> None:
    with SessionLocal() as session:
        session.add(
            Clients(
                client_id=COLLIDING_CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance=42.0,
                n_purchases_returned=7,
                n_sales_returned=1,
            )
        )
        session.add(_fund_row(COLLIDING_CLIENT_ID, FUND_ID, balance=5_000.0))
        session.add(PiiVault(client_id=COLLIDING_CLIENT_ID, client_name="Existing Client"))
        session.commit()

    try:
        with SessionLocal() as session:
            enroll_auto_checkin_clients(session, [COLLIDING_CLIENT_ID])
            session.commit()

            client_row = session.get(Clients, COLLIDING_CLIENT_ID)
            features = session.get(ClientFeatures, COLLIDING_CLIENT_ID)
            indicator = session.get(ClientMessageIndicators, COLLIDING_CLIENT_ID)

        assert client_row.balance == 42.0
        assert client_row.n_purchases_returned == 7
        assert features.active_book_auto_checkin is True
        assert indicator.message_angle == AUTO_CHECKIN_ANGLE
    finally:
        with SessionLocal() as session:
            campaign_id = session.scalar(
                select(Campaign.campaign_id).where(
                    Campaign.campaign_type == AUTO_CHECKIN_CAMPAIGN_TYPE
                )
            )
            _cleanup_client(session, COLLIDING_CLIENT_ID, campaign_id)
            session.commit()


def test_no_seeded_campaign_returns_empty(campaign_seed: int, monkeypatch) -> None:
    monkeypatch.setattr(nurture_bridge_mod, "_find_campaign", lambda session: None)
    with SessionLocal() as session:
        session.add(_fund_row(CLIENT_ID_2, FUND_ID, balance=5_000.0))
        session.add(PiiVault(client_id=CLIENT_ID_2, client_name="No Campaign Client"))
        session.commit()

        bridged = enroll_auto_checkin_clients(session, [CLIENT_ID_2])
        session.commit()

    assert bridged == []

    with SessionLocal() as session:
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT_ID_2))
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID_2))
        session.commit()


def _accepted_state(client_id: int) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": AUTO_CHECKIN_ANGLE,
        "prompt_variant": AUTO_CHECKIN_ANGLE,
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {"subject": "s", "body": "b"},
    }


def _make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _fake_generate(session, enrollment, step_no) -> OutreachMessage:
    run = persist_generation_run(session, _accepted_state(enrollment.client_id), _make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=enrollment.campaign_id,
        generation_run_id=run.run_id,
        client_id=enrollment.client_id,
        ai_draft_content={"subject": "s", "body": "b"},
        personalized_content={"subject": "s", "body": "b"},
    )
    session.add(message)
    session.flush()
    return message


def test_enroll_generate_approve_send_for_an_auto_checkin_client(active_client) -> None:
    client_id, campaign_id = active_client
    with SessionLocal() as session:
        enroll_auto_checkin_clients(session, [client_id])
        session.commit()

        outcomes = run_due_enrollments(session, campaign_id=campaign_id, generate=_fake_generate)
        session.commit()

    assert len(outcomes) == 1
    assert outcomes[0].generated is True

    with SessionLocal() as session:
        touch = session.get(TouchLog, outcomes[0].touch_id)
        session.get(OutreachMessage, touch.message_id).status = "approved"
        session.commit()

        touch = session.get(TouchLog, outcomes[0].touch_id)
        sent = send_touch(session, touch)
        session.commit()

    assert sent.sent_at is not None
    assert sent.delivery_status == "stubbed"

    with SessionLocal() as session:
        assert select_due_enrollments(session, campaign_id=campaign_id) == []


@pytest.fixture
def unconfigured_mailer(monkeypatch) -> NullMailer:
    mailer = NullMailer(sender="ace@example.com")
    monkeypatch.setattr(sender_module, "get_mailer", lambda *args, **kwargs: mailer)
    return mailer


def test_auto_checkin_campaign_reachable_through_the_real_api(
    active_client, configured_reviewers, reviewer_1_headers, unconfigured_mailer: NullMailer
) -> None:
    client_id, campaign_id = active_client
    with SessionLocal() as session:
        enroll_auto_checkin_clients(session, [client_id])
        session.commit()

    detail = api_client.get(f"{CAMPAIGNS}/{campaign_id}")
    assert detail.status_code == 200
    assert detail.json()["campaign_type"] == AUTO_CHECKIN_CAMPAIGN_TYPE

    enrollments = api_client.get(f"{CAMPAIGNS}/{campaign_id}/enrollments")
    assert enrollments.status_code == 200
    rows_by_client = {row["client_id"]: row for row in enrollments.json()["items"]}
    assert rows_by_client[client_id]["message_angle"] == AUTO_CHECKIN_ANGLE

    with SessionLocal() as session:
        outcomes = run_due_enrollments(session, campaign_id=campaign_id, generate=_fake_generate)
        session.commit()
    assert outcomes[0].generated is True

    with SessionLocal() as session:
        touch = session.get(TouchLog, outcomes[0].touch_id)
        message_id = touch.message_id

    review = api_client.get(f"{REVIEWS}/{message_id}")
    assert review.status_code == 200
    assert review.json()["client_id"] == client_id

    decision = api_client.post(
        f"{REVIEWS}/{message_id}/decide",
        json={"outcome": "approve"},
        headers=reviewer_1_headers,
    )
    assert decision.status_code == 200

    sent = api_client.post(f"{CAMPAIGNS}/{campaign_id}/send")
    assert sent.status_code == 200
    outcomes_json = sent.json()
    assert len(outcomes_json) == 1
    assert outcomes_json[0]["sent"] is True
