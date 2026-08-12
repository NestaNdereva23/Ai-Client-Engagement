"""End-to-end campaign orchestration: enroll, schedule, gate, and run a
batch through review with a stubbed sender.

Ties every M9 piece together the way a real daily run would: enroll_cohort
creates the enrollment, run_due_enrollments generates a touch once one is
due and eligible, an approval stands in for the review queue, and
send_touch delivers through the stub and advances the schedule. Time does
not actually pass in a test, so next_due_at is pushed into the past
directly between steps rather than waiting for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.campaigns.enrollment import enroll_cohort
from app.campaigns.scheduler import select_due_enrollments
from app.campaigns.touch import run_due_enrollments, send_touch
from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run

_FUND_ID = 998
_CLIENT_ID = 99801


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
        "raw_structured_output": {"subject": "s", "body": "b"},
    }


def fake_generate(session, enrollment, step_no) -> OutreachMessage:
    """Stands in for the real agent pipeline: a plain accepted run and message."""
    run = persist_generation_run(session, accepted_state(enrollment.client_id), make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=enrollment.campaign_id,
        generation_run_id=run.run_id,
        client_id=enrollment.client_id,
        ai_draft_content={"subject": "s", "body": "b"},
    )
    session.add(message)
    session.flush()
    return message


def _approve(session, message_id: str) -> None:
    session.get(OutreachMessage, message_id).status = "approved"
    session.flush()


def _run_one_touch(session, campaign_id: int) -> TouchLog:
    """Generate the next due touch for campaign_id, approve it, and send it."""
    outcomes = run_due_enrollments(session, campaign_id=campaign_id, generate=fake_generate)
    session.commit()
    assert len(outcomes) == 1
    assert outcomes[0].generated is True

    touch = session.get(TouchLog, outcomes[0].touch_id)
    _approve(session, touch.message_id)
    session.commit()

    sent = send_touch(session, touch)
    session.commit()
    return sent


@pytest.fixture
def weekly_campaign(db: None):
    """Four steps, one per week: the design's own "weekly for a month" example."""
    with SessionLocal() as session:
        campaign = Campaign(name="test weekly campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        for step_no, offset_days in enumerate((0, 7, 14, 21), start=1):
            session.add(
                CampaignStep(
                    campaign_id=campaign_id,
                    step_no=step_no,
                    offset_days=offset_days,
                    message_angle="winback_habit",
                )
            )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.campaign_id == campaign_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(
            delete(AuditLog).where(AuditLog.entity_type.in_(("enrollment", "touch_log")))
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def client_row(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=_FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=_CLIENT_ID,
                unit_fund_id=_FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(
            PiiVault(client_id=_CLIENT_ID, client_name="Test Client", contact_email="a@example.com")
        )
        session.commit()

    yield _CLIENT_ID

    with SessionLocal() as session:
        # Torn down before `weekly_campaign` (fixtures unwind in reverse setup
        # order), so anything still referencing this client must go first.
        session.execute(delete(ContactEvent).where(ContactEvent.client_id == _CLIENT_ID))
        session.execute(delete(Suppression).where(Suppression.client_id == _CLIENT_ID))
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.client_id == _CLIENT_ID)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.client_id == _CLIENT_ID))
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.client_id == _CLIENT_ID)
        ).all()
        if message_ids:
            session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(
                GenerationRun.run_id.in_(
                    select(OutreachMessage.generation_run_id).where(
                        OutreachMessage.client_id == _CLIENT_ID
                    )
                )
            )
        ).all()
        session.execute(delete(OutreachMessage).where(OutreachMessage.client_id == _CLIENT_ID))
        if run_ids:
            session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id == _CLIENT_ID))
        session.execute(delete(Clients).where(Clients.client_id == _CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND_ID))
        session.commit()


def test_weekly_for_a_month_sequencing_runs_all_four_steps(
    weekly_campaign: int, client_row: int
) -> None:
    with SessionLocal() as session:
        [enrollment] = enroll_cohort(session, campaign_id=weekly_campaign, client_ids=[client_row])
        session.commit()
        enrollment_id = enrollment.enrollment_id

    for step_no in range(1, 5):
        with SessionLocal() as session:
            touch = _run_one_touch(session, weekly_campaign)
            assert touch.step_no == step_no

            row = session.get(Enrollment, enrollment_id)
            assert row.current_step == step_no
            if step_no < 4:
                assert row.status == "in_progress"
                assert row.next_due_at is not None
                # Simulate the wait until the next step is actually due, and
                # backdate the touch just sent so cooldown does not still
                # see it as recent.
                row.next_due_at = datetime.now(UTC) - timedelta(minutes=1)
                touch.sent_at = datetime.now(UTC) - timedelta(days=8)
                session.commit()
            else:
                assert row.status == "completed"
                assert row.next_due_at is None

    with SessionLocal() as session:
        touches = (
            session.execute(
                select(TouchLog)
                .where(TouchLog.enrollment_id == enrollment_id)
                .order_by(TouchLog.step_no)
            )
            .scalars()
            .all()
        )
    assert [t.step_no for t in touches] == [1, 2, 3, 4]
    assert all(t.sent_at is not None for t in touches)


def test_a_second_run_before_advancing_creates_no_duplicate_touch(
    weekly_campaign: int, client_row: int
) -> None:
    with SessionLocal() as session:
        [enrollment] = enroll_cohort(session, campaign_id=weekly_campaign, client_ids=[client_row])
        session.commit()

        first = run_due_enrollments(session, campaign_id=weekly_campaign, generate=fake_generate)
        session.commit()
        second = run_due_enrollments(session, campaign_id=weekly_campaign, generate=fake_generate)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert first[0].generated is True
    # A step with a touch already logged is excluded from selection itself
    # now, not selected and then filtered out, so the second run finds
    # nothing left due.
    assert second == []

    with SessionLocal() as session:
        touches = (
            session.execute(select(TouchLog).where(TouchLog.enrollment_id == enrollment_id))
            .scalars()
            .all()
        )
    assert len(touches) == 1


def test_cooldown_blocks_a_second_campaign_right_after_a_send(
    weekly_campaign: int, client_row: int
) -> None:
    with SessionLocal() as session:
        other = Campaign(name="second campaign for cooldown")
        session.add(other)
        session.commit()
        other_id = other.campaign_id
        session.add(
            CampaignStep(
                campaign_id=other_id, step_no=1, offset_days=0, message_angle="winback_value"
            )
        )
        session.commit()

        enroll_cohort(session, campaign_id=weekly_campaign, client_ids=[client_row])
        session.commit()
        _run_one_touch(session, weekly_campaign)

        enroll_cohort(session, campaign_id=other_id, client_ids=[client_row])
        session.commit()

        outcomes = run_due_enrollments(session, campaign_id=other_id, generate=fake_generate)
        session.commit()

    assert outcomes[0].generated is False
    assert outcomes[0].reason == "cooldown"

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == other_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == other_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == other_id))
        session.commit()


def test_a_reply_after_the_first_touch_stops_the_sequence(
    weekly_campaign: int, client_row: int
) -> None:
    with SessionLocal() as session:
        [enrollment] = enroll_cohort(session, campaign_id=weekly_campaign, client_ids=[client_row])
        session.commit()
        enrollment_id = enrollment.enrollment_id

        _run_one_touch(session, weekly_campaign)

        session.add(ContactEvent(client_id=client_row, type="reply", occurred_at=datetime.now(UTC)))
        row = session.get(Enrollment, enrollment_id)
        row.next_due_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

        outcomes = run_due_enrollments(session, campaign_id=weekly_campaign, generate=fake_generate)
        session.commit()

    assert outcomes[0].generated is False
    assert outcomes[0].reason == "replied"

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.status == "stopped_reply"
        assert select_due_enrollments(session, campaign_id=weekly_campaign) == []


def test_an_opt_out_after_the_first_touch_stops_the_sequence(
    weekly_campaign: int, client_row: int
) -> None:
    with SessionLocal() as session:
        [enrollment] = enroll_cohort(session, campaign_id=weekly_campaign, client_ids=[client_row])
        session.commit()
        enrollment_id = enrollment.enrollment_id

        _run_one_touch(session, weekly_campaign)

        session.add(Suppression(client_id=client_row, reason="unsubscribe"))
        row = session.get(Enrollment, enrollment_id)
        row.next_due_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

        outcomes = run_due_enrollments(session, campaign_id=weekly_campaign, generate=fake_generate)
        session.commit()

    assert outcomes[0].generated is False
    assert outcomes[0].reason == "suppressed"

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.status == "stopped_optout"
        assert select_due_enrollments(session, campaign_id=weekly_campaign) == []
