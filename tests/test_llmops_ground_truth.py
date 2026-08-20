"""Ground truth rows: a terminal review decision paired with its judge scores.

Covers the join surfacing angle and tier from the label review already
stamped, a run the judge never scored still appearing with null scores, and
slicing by angle/tier so the twelve angles can be compared against each
other.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.campaigns.cohorts import CohortSlot
from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import Evaluation, GenerationRun
from app.db.models.message_template import MessageTemplate, TemplateReviewAction
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.ground_truth import ground_truth_rows, template_ground_truth_rows
from app.llmops.versions import persist_evaluation, persist_generation_run
from app.schemas.evaluation import EvaluationScores
from app.services.review import create_outreach_message, decide
from app.services.template_review import decide_template


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
        "judge_llm_provider": "",
        "judge_llm_model": "",
        "judge_llm_temperature": None,
        "judge_llm_max_tokens": 512,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_state(client_id: int, *, angle: str, priority_tier: str) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": angle,
        "priority_tier": priority_tier,
        "prompt_variant": angle,
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
def scenario(roles):
    """A client and a campaign, ready to grow generation runs and messages on demand."""
    fund_id = 980
    client_id = 98001
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
        campaign = Campaign(name="ground truth test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

    yield client_id, campaign_id

    with SessionLocal() as session:
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id == client_id)
        ).all()
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.client_id == client_id)
        ).all()
        if message_ids:
            session.execute(delete(AuditLog).where(AuditLog.entity_id.in_(message_ids)))
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
        if run_ids:
            template_ids = session.scalars(
                select(MessageTemplate.template_id).where(
                    MessageTemplate.generation_run_id.in_(run_ids)
                )
            ).all()
            if template_ids:
                session.execute(
                    delete(TemplateReviewAction).where(
                        TemplateReviewAction.template_id.in_(template_ids)
                    )
                )
                session.execute(
                    delete(MessageTemplate).where(MessageTemplate.template_id.in_(template_ids))
                )
            session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
            session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def _reviewed_message(
    session, client_id: int, campaign_id: int, *, angle: str, priority_tier: str, outcome: str
) -> str:
    run = persist_generation_run(
        session, make_state(client_id, angle=angle, priority_tier=priority_tier), make_settings()
    )
    session.commit()
    # This helper drives every review outcome, including reject/escalate/
    # hold; a cohort message only ever allows approve/edit_approve, so
    # opt out of cohort sampling here -- ground-truth export is what's
    # under test, not that restriction.
    message = create_outreach_message(
        session, run, campaign_id=campaign_id, cohort_slot=CohortSlot(None, False)
    )
    session.commit()
    decide(session, message.message_id, outcome=outcome, reviewer_id="fa-1")
    session.commit()
    return message.message_id, run.run_id


def test_ground_truth_rows_pairs_the_label_with_the_judges_scores(scenario) -> None:
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        message_id, run_id = _reviewed_message(
            session,
            client_id,
            campaign_id,
            angle="winback_habit",
            priority_tier="T2",
            outcome="approve",
        )
        run = session.get(GenerationRun, run_id)
        persist_evaluation(
            session,
            run,
            EvaluationScores(tone=4, compliance=5, grounding=5, personalization=3, notes="fine"),
            make_settings(),
        )
        session.commit()

    with SessionLocal() as session:
        rows = ground_truth_rows(session, message_angle="winback_habit", priority_tier="T2")

    matching = [r for r in rows if r.message_id == message_id]
    assert len(matching) == 1
    row = matching[0]
    assert row.outcome == "approve"
    assert row.message_angle == "winback_habit"
    assert row.priority_tier == "T2"
    assert row.tone == 4


def test_ground_truth_rows_includes_a_run_with_no_evaluation_as_a_null_score(scenario) -> None:
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        message_id, _run_id = _reviewed_message(
            session,
            client_id,
            campaign_id,
            angle="winback_habit",
            priority_tier="T2",
            outcome="reject",
        )

    with SessionLocal() as session:
        rows = ground_truth_rows(session, message_angle="winback_habit", priority_tier="T2")

    matching = [r for r in rows if r.message_id == message_id]
    assert len(matching) == 1
    assert matching[0].tone is None
    assert matching[0].outcome == "reject"


def test_ground_truth_rows_filters_out_a_different_angle(scenario) -> None:
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        message_id, _run_id = _reviewed_message(
            session,
            client_id,
            campaign_id,
            angle="pick_up_again",
            priority_tier="T3",
            outcome="approve",
        )

    with SessionLocal() as session:
        rows = ground_truth_rows(session, message_angle="winback_habit")

    assert message_id not in [r.message_id for r in rows]


def _reviewed_template(
    session, client_id: int, campaign_id: int, *, angle: str, priority_tier: str, outcome: str
) -> tuple[str, str]:
    """A template's run is stamped with one representative client, since
    generation_runs.client_id is a real FK."""
    run = persist_generation_run(
        session, make_state(client_id, angle=angle, priority_tier=priority_tier), make_settings()
    )
    session.commit()
    template = MessageTemplate(
        template_id=str(uuid4()),
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        profile_key={"message_angle": angle, "priority_tier": priority_tier},
        ai_draft_content={
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}.",
        },
    )
    session.add(template)
    session.commit()
    decide_template(session, template.template_id, outcome=outcome, reviewer_id="fa-1")
    session.commit()
    return template.template_id, run.run_id


def test_template_ground_truth_rows_pairs_the_label_with_the_judges_scores(scenario) -> None:
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        template_id, run_id = _reviewed_template(
            session,
            client_id,
            campaign_id,
            angle="winback_habit",
            priority_tier="T2",
            outcome="approve",
        )
        run = session.get(GenerationRun, run_id)
        persist_evaluation(
            session,
            run,
            EvaluationScores(tone=4, compliance=5, grounding=5, personalization=3, notes="fine"),
            make_settings(),
        )
        session.commit()

    with SessionLocal() as session:
        rows = template_ground_truth_rows(
            session, message_angle="winback_habit", priority_tier="T2"
        )

    matching = [r for r in rows if r.template_id == template_id]
    assert len(matching) == 1
    row = matching[0]
    assert row.outcome == "approve"
    assert row.tone == 4


def test_template_ground_truth_rows_includes_a_run_with_no_evaluation_as_a_null_score(
    scenario,
) -> None:
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        template_id, _run_id = _reviewed_template(
            session,
            client_id,
            campaign_id,
            angle="winback_habit",
            priority_tier="T2",
            outcome="reject",
        )

    with SessionLocal() as session:
        rows = template_ground_truth_rows(
            session, message_angle="winback_habit", priority_tier="T2"
        )

    matching = [r for r in rows if r.template_id == template_id]
    assert len(matching) == 1
    assert matching[0].tone is None
    assert matching[0].outcome == "reject"


def test_template_ground_truth_rows_stays_one_to_one_however_many_instances_exist(
    scenario,
) -> None:
    """Instantiating many messages never multiplies the template's own
    ground-truth row."""
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        template_id, run_id = _reviewed_template(
            session,
            client_id,
            campaign_id,
            angle="winback_habit",
            priority_tier="T2",
            outcome="approve",
        )
        run = session.get(GenerationRun, run_id)
        for _ in range(3):
            create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()

    with SessionLocal() as session:
        rows = template_ground_truth_rows(
            session, message_angle="winback_habit", priority_tier="T2"
        )

    assert len([r for r in rows if r.template_id == template_id]) == 1


def test_ground_truth_rows_excludes_escalate_and_hold(scenario) -> None:
    client_id, campaign_id = scenario
    with SessionLocal() as session:
        message_id, _run_id = _reviewed_message(
            session,
            client_id,
            campaign_id,
            angle="winback_habit",
            priority_tier="T2",
            outcome="escalate",
        )

    with SessionLocal() as session:
        rows = ground_truth_rows(session, message_angle="winback_habit", priority_tier="T2")

    assert message_id not in [r.message_id for r in rows]
