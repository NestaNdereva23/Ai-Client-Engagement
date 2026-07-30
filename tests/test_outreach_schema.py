"""The outreach workflow schema: campaign, outreach_message, review_action.

These prove each CHECK constraint rejects a value outside its enumerated
set, an outreach_message round-trips both content columns, a generation run
can back at most one outreach_message, and review_action accumulates a full
history for one message rather than being limited to a single row.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run


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
def generation_run(db: None):
    """A fund, a client, and one accepted generation run to hang a message off."""
    fund_id = 960
    client_id = 96001
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        run = persist_generation_run(session, accepted_state(client_id), make_settings())
        session.commit()
        run_id = run.run_id

    yield client_id, run_id

    with SessionLocal() as session:
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.generation_run_id == run_id)
        ).all()
        if message_ids:
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


@pytest.fixture
def campaign(db: None):
    with SessionLocal() as session:
        row = Campaign(name="test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_campaign_defaults_to_draft_status(campaign: int) -> None:
    with SessionLocal() as session:
        row = session.get(Campaign, campaign)
        assert row.status == "draft"
        assert row.campaign_type == "dormant_reengagement"


def test_campaign_status_check_constraint_rejects_an_invalid_value(db: None) -> None:
    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(Campaign(name="bad", status="not_a_real_status"))
        session.commit()


def test_outreach_message_round_trips_ai_draft_and_personalized_content(
    campaign: int, generation_run
) -> None:
    client_id, run_id = generation_run
    message_id = uuid4().hex
    with SessionLocal() as session:
        session.add(
            OutreachMessage(
                message_id=message_id,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
                personalized_content={"subject": "s", "body": "Dear Jane, b"},
            )
        )
        session.commit()

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert stored.ai_draft_content == {"subject": "s", "body": "b"}
        assert stored.personalized_content == {"subject": "s", "body": "Dear Jane, b"}
        assert stored.status == "pending_review"
        assert stored.channel == "email"


def test_outreach_message_personalized_content_may_start_null(
    campaign: int, generation_run
) -> None:
    """Re-attachment (M8.2) fills this in after the row is created."""
    client_id, run_id = generation_run
    message_id = uuid4().hex
    with SessionLocal() as session:
        session.add(
            OutreachMessage(
                message_id=message_id,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
            )
        )
        session.commit()

    with SessionLocal() as session:
        assert session.get(OutreachMessage, message_id).personalized_content is None


def test_outreach_message_status_check_constraint_rejects_an_invalid_value(
    campaign: int, generation_run
) -> None:
    client_id, run_id = generation_run
    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            OutreachMessage(
                message_id=uuid4().hex,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
                status="not_a_real_status",
            )
        )
        session.commit()


def test_a_generation_run_backs_at_most_one_outreach_message(campaign: int, generation_run) -> None:
    client_id, run_id = generation_run
    with SessionLocal() as session:
        session.add(
            OutreachMessage(
                message_id=uuid4().hex,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
            )
        )
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            OutreachMessage(
                message_id=uuid4().hex,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
            )
        )
        session.commit()


def test_review_action_outcome_check_constraint_rejects_an_invalid_value(
    campaign: int, generation_run
) -> None:
    client_id, run_id = generation_run
    message_id = uuid4().hex
    with SessionLocal() as session:
        session.add(
            OutreachMessage(
                message_id=message_id,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
            )
        )
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            ReviewAction(message_id=message_id, reviewer_id="fa-1", outcome="not_a_real_outcome")
        )
        session.commit()


def test_review_action_accumulates_a_full_history_for_one_message(
    campaign: int, generation_run
) -> None:
    """escalate/hold are waypoints, not dead ends: a message can carry more
    than one action over its lifetime, and review_action keeps every one."""
    client_id, run_id = generation_run
    message_id = uuid4().hex
    with SessionLocal() as session:
        session.add(
            OutreachMessage(
                message_id=message_id,
                campaign_id=campaign,
                generation_run_id=run_id,
                client_id=client_id,
                ai_draft_content={"subject": "s", "body": "b"},
            )
        )
        session.commit()

        session.add(ReviewAction(message_id=message_id, reviewer_id="fa-1", outcome="escalate"))
        session.add(
            ReviewAction(
                message_id=message_id,
                reviewer_id="lead-1",
                outcome="edit_approve",
                edited_content={"subject": "s", "body": "edited body"},
            )
        )
        session.commit()

    with SessionLocal() as session:
        actions = session.scalars(
            select(ReviewAction)
            .where(ReviewAction.message_id == message_id)
            .order_by(ReviewAction.created_at)
        ).all()

    assert [a.outcome for a in actions] == ["escalate", "edit_approve"]
    assert actions[1].edited_content == {"subject": "s", "body": "edited body"}
