"""The send gate: nothing sends without approval, and every decision audits.

Statuses are reached through the real review workflow (decide()), not set by
hand, so this proves the gate against what the review queue can actually
produce, not a synthetic state the app could never leave a message in.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.delivery.gate import MessageNotApproved, authorize_send
from app.llmops.versions import persist_generation_run
from app.services.review import create_outreach_message, decide


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def accepted_state(client_id: int) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "winback_habit",
        "prompt_variant": "habit_premium",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


@pytest.fixture
def message(roles):
    """A pending_review outreach_message backed by a real client, fund, and run."""
    fund_id = 972
    client_id = 97201
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
        session.commit()
        run = persist_generation_run(session, accepted_state(client_id), make_settings())
        campaign = Campaign(name="delivery gate test campaign")
        session.add(campaign)
        session.commit()
        created = create_outreach_message(session, run, campaign_id=campaign.campaign_id)
        session.commit()
        message_id, run_id, campaign_id_val = created.message_id, run.run_id, campaign.campaign_id

    yield message_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == message_id))
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id_val))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_a_still_pending_message_is_refused(message) -> None:
    with SessionLocal() as session:
        outreach_message = session.get(OutreachMessage, message)
        with pytest.raises(MessageNotApproved):
            authorize_send(session, outreach_message)
        session.commit()


@pytest.mark.parametrize("outcome", ["reject", "escalate", "hold"])
def test_a_non_approved_decision_is_refused(message, outcome) -> None:
    with SessionLocal() as session:
        decide(session, message, outcome=outcome, reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session:
        outreach_message = session.get(OutreachMessage, message)
        with pytest.raises(MessageNotApproved):
            authorize_send(session, outreach_message)
        session.commit()


def test_an_approved_message_is_authorized(message) -> None:
    with SessionLocal() as session:
        decide(session, message, outcome="approve", reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session:
        outreach_message = session.get(OutreachMessage, message)
        authorized = authorize_send(session, outreach_message)
        session.commit()
    assert authorized.message_id == message


def test_a_refusal_audits_a_denied_gate_decision(message) -> None:
    with SessionLocal() as session:
        outreach_message = session.get(OutreachMessage, message)
        with pytest.raises(MessageNotApproved):
            authorize_send(session, outreach_message)
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == message, AuditLog.action == "send_gate_denied"
            )
        )
    assert row is not None
    assert row.detail["status"] == "pending_review"


def test_an_authorization_audits_an_allowed_gate_decision(message) -> None:
    with SessionLocal() as session:
        decide(session, message, outcome="approve", reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session:
        outreach_message = session.get(OutreachMessage, message)
        authorize_send(session, outreach_message)
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_id == message, AuditLog.action == "send_gate_allowed"
            )
        )
    assert row is not None
