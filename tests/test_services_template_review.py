"""The template review workflow: the queue, and reviewer decisions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.message_template import MessageTemplate, TemplateReviewAction
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.services.template_review import (
    EditedContentRequired,
    InvalidOutcome,
    TemplateAlreadyDecided,
    TemplateNotFound,
    compute_edit_diff,
    decide_template,
    get_template,
    get_template_review_history,
    list_pending_templates,
)


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
        "angle": "pick_up_again",
        "prompt_variant": "pick_up_again",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, your typical contribution was {{typical_contribution}}.",
        },
    }


PROFILE_KEY = {
    "message_angle": "pick_up_again",
    "priority_tier": "T3",
    "product": "money market",
    "has_cadence": True,
    "stale_contact": False,
    "exit_reason_charge_settled": False,
    "fund_name_known": False,
}

FUND_ID = 9730
CLIENT_ID = 973001


@pytest.fixture
def run(db: None):
    """A fund, a client, and one accepted generation run to hang a template off."""
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        row = persist_generation_run(session, accepted_state(CLIENT_ID), make_settings())
        session.commit()
        run_id = row.run_id

    yield run_id

    with SessionLocal() as session:
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


@pytest.fixture
def campaign(db: None):
    with SessionLocal() as session:
        row = Campaign(name="template review test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def make_template(campaign_id: int, run_id: str) -> MessageTemplate:
    return MessageTemplate(
        template_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run_id,
        profile_key=PROFILE_KEY,
        ai_draft_content={
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, your typical contribution was {{typical_contribution}}.",
        },
    )


@pytest.fixture
def template(campaign: int, run: str):
    with SessionLocal() as session:
        row = make_template(campaign, run)
        session.add(row)
        session.commit()
        template_id = row.template_id

    yield template_id, campaign

    with SessionLocal() as session:
        session.execute(
            delete(TemplateReviewAction).where(TemplateReviewAction.template_id == template_id)
        )
        session.execute(delete(MessageTemplate).where(MessageTemplate.template_id == template_id))
        session.commit()


def test_get_template_raises_when_not_found(db: None) -> None:
    with pytest.raises(TemplateNotFound):
        with SessionLocal() as session:
            get_template(session, "does-not-exist")


def test_template_defaults_to_pending_review(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        row = get_template(session, template_id)
    assert row.status == "pending_review"


def test_list_pending_templates_returns_the_new_template(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        pending, _cursor = list_pending_templates(session)
    assert template_id in [t.template_id for t in pending]


def test_list_pending_templates_filters_by_campaign(template) -> None:
    template_id, campaign_id = template
    with SessionLocal() as session:
        matched, _cursor = list_pending_templates(session, campaign_id=campaign_id)
        unmatched, _cursor = list_pending_templates(session, campaign_id=campaign_id + 1)
    assert template_id in [t.template_id for t in matched]
    assert template_id not in [t.template_id for t in unmatched]


def test_list_pending_templates_filters_by_status(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        approved, _cursor = list_pending_templates(session, status="approved")
    assert template_id not in [t.template_id for t in approved]


def test_decide_approve_moves_status_to_approved(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        decide_template(session, template_id, outcome="approve", reviewer_id="reviewer-1")
        session.commit()
        row = get_template(session, template_id)
    assert row.status == "approved"


def test_decide_reject_moves_status_to_rejected(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        decide_template(session, template_id, outcome="reject", reviewer_id="reviewer-1")
        session.commit()
        row = get_template(session, template_id)
    assert row.status == "rejected"


def test_decide_escalate_then_approve_is_allowed(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        decide_template(session, template_id, outcome="escalate", reviewer_id="reviewer-1")
        session.commit()
        row = get_template(session, template_id)
        assert row.status == "escalated"

        decide_template(session, template_id, outcome="approve", reviewer_id="reviewer-2")
        session.commit()
        row = get_template(session, template_id)
    assert row.status == "approved"


def test_decide_on_an_already_approved_template_raises(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        decide_template(session, template_id, outcome="approve", reviewer_id="reviewer-1")
        session.commit()

        with pytest.raises(TemplateAlreadyDecided):
            decide_template(session, template_id, outcome="reject", reviewer_id="reviewer-2")


def test_decide_with_an_invalid_outcome_raises(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session, pytest.raises(InvalidOutcome):
        decide_template(session, template_id, outcome="not_a_real_outcome", reviewer_id="r1")


def test_decide_edit_approve_requires_edited_content(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session, pytest.raises(EditedContentRequired):
        decide_template(session, template_id, outcome="edit_approve", reviewer_id="r1")


def test_decide_edit_approve_overwrites_ai_draft_content_and_records_a_diff(template) -> None:
    template_id, _campaign_id = template
    edited = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, your usual contribution was {{typical_contribution}}.",
    }
    with SessionLocal() as session:
        action = decide_template(
            session,
            template_id,
            outcome="edit_approve",
            reviewer_id="reviewer-1",
            edited_content=edited,
        )
        session.commit()
        row = get_template(session, template_id)

    assert row.status == "approved"
    assert row.ai_draft_content == edited
    assert "body" in action.edit_diff
    assert "subject" not in action.edit_diff


def test_decide_stamps_message_angle_and_priority_tier_from_the_profile_key(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        action = decide_template(session, template_id, outcome="approve", reviewer_id="r1")
    assert action.message_angle == "pick_up_again"
    assert action.priority_tier == "T3"


def test_get_template_review_history_accumulates_every_decision(template) -> None:
    template_id, _campaign_id = template
    with SessionLocal() as session:
        decide_template(session, template_id, outcome="escalate", reviewer_id="r1")
        decide_template(session, template_id, outcome="approve", reviewer_id="r2")
        session.commit()
        history = get_template_review_history(session, template_id)
    assert [a.outcome for a in history] == ["escalate", "approve"]


def test_compute_edit_diff_only_includes_changed_fields() -> None:
    diff = compute_edit_diff(
        {"subject": "same", "body": "old body"}, {"subject": "same", "body": "new body"}
    )
    assert "subject" not in diff
    assert "body" in diff
