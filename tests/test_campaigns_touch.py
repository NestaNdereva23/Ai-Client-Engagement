"""Recording a touch, generating it through review, sending it, and
reconciling touch_log against enrollment state.

record_touch and generate_touch cover the idempotent-insert-before-send
guarantee directly: a repeated call finds the same row and, once a
message exists, never calls generate again. run_due_enrollments covers
the gate-then-generate batch path ending at pending_review, never
auto-advancing the enrollment. send_touch covers the happy path (advance,
audit) and the send-time recheck actually blocking a delivery.
reconcile_enrollment covers catching current_step up after a simulated
crash between send and advance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.campaigns.touch import (
    SendBlocked,
    generate_touch,
    reconcile_enrollment,
    record_touch,
    run_due_enrollments,
    send_touch,
)
from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run

_FUND_ID = 997


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


def _make_message(
    session, *, campaign_id: int, client_id: int, status: str = "pending_review"
) -> OutreachMessage:
    run = persist_generation_run(session, accepted_state(client_id), make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "s", "body": "b"},
        status=status,
    )
    session.add(message)
    session.flush()
    return message


@pytest.fixture
def campaign_with_steps(db: None):
    with SessionLocal() as session:
        campaign = Campaign(name="test touch campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="winback_habit"
            )
        )
        session.add(
            CampaignStep(
                campaign_id=campaign_id, step_no=2, offset_days=7, message_angle="winback_value"
            )
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        # Runs after `client_row`'s own teardown (fixtures unwind in reverse
        # setup order, and client_row is requested second in every test here),
        # so any touch_log row still pointing at a message must be a row
        # client_row's own cleanup did not already know about (no message yet).
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
    fund_id = _FUND_ID
    client_id = 99701
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
        session.add(
            PiiVault(client_id=client_id, client_name="Test Client", contact_email="a@example.com")
        )
        session.commit()

    yield client_id

    with SessionLocal() as session:
        # Torn down before `campaign_with_steps` (fixtures unwind in reverse
        # setup order), so anything still referencing this client (an
        # enrollment, or a touch_log row pointing at one of its messages)
        # must go first or the FKs on clients/outreach_message block it.
        session.execute(delete(Suppression).where(Suppression.client_id == client_id))
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.client_id == client_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.client_id == client_id))
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.client_id == client_id)
        ).all()
        if message_ids:
            session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
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
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


@pytest.fixture
def second_client_row(db: None):
    """A second client_id, a different fund, standing in for the same real person."""
    fund_id = 998
    client_id = 99702
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund 2"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(
            PiiVault(
                client_id=client_id, client_name="Test Client Two", contact_email="b@example.com"
            )
        )
        session.commit()

    yield client_id

    with SessionLocal() as session:
        # Mirrors client_row's teardown: an outreach_message or generation_run
        # this client's own touch produced must go before the campaign, client,
        # and fund it references, or the FKs block every delete after it.
        session.execute(delete(Suppression).where(Suppression.client_id == client_id))
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.client_id == client_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.client_id == client_id))
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.client_id == client_id)
        ).all()
        if message_ids:
            session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
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
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def _make_enrollment(session, *, campaign_id: int, client_id: int, **overrides) -> Enrollment:
    row = Enrollment(campaign_id=campaign_id, client_id=client_id, **overrides)
    session.add(row)
    session.commit()
    return row


def test_record_touch_is_a_no_op_on_a_repeated_call(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        first = record_touch(session, enrollment, 1)
        session.commit()
        second = record_touch(session, enrollment, 1)
        session.commit()

        assert first.touch_id == second.touch_id
        rows = (
            session.execute(
                select(TouchLog).where(TouchLog.enrollment_id == enrollment.enrollment_id)
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


def test_generate_touch_only_calls_generate_once_per_step(
    campaign_with_steps: int, client_row: int
) -> None:
    calls = []

    def fake_generate(session, enrollment, step_no):
        calls.append(step_no)
        return _make_message(session, campaign_id=campaign_with_steps, client_id=client_row)

    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        generate_touch(session, enrollment, 1, generate=fake_generate)
        session.commit()
        generate_touch(session, enrollment, 1, generate=fake_generate)
        session.commit()

    assert calls == [1]


def test_run_due_enrollments_generates_a_pending_review_message_without_advancing(
    campaign_with_steps: int, client_row: int
) -> None:
    def fake_generate(session, enrollment, step_no):
        return _make_message(session, campaign_id=campaign_with_steps, client_id=client_row)

    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        outcomes = run_due_enrollments(
            session, campaign_id=campaign_with_steps, generate=fake_generate
        )
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert len(outcomes) == 1
    assert outcomes[0].generated is True

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 0
        assert row.status == "enrolled"

        touch = session.scalar(
            select(TouchLog).where(TouchLog.enrollment_id == enrollment_id, TouchLog.step_no == 1)
        )
        assert touch.sent_at is None
        message = session.get(OutreachMessage, touch.message_id)
        assert message.status == "pending_review"


def test_run_due_enrollments_generates_exactly_one_touch_for_a_dual_fund_client(
    campaign_with_steps: int, client_row: int, second_client_row: int
) -> None:
    """Two enrollment rows for the same real person, one per fund: only the
    primary row's touch is ever generated, matching is_primary_contact_row."""
    calls = []

    def fake_generate(session, enrollment, step_no):
        calls.append(enrollment.enrollment_id)
        return _make_message(
            session, campaign_id=campaign_with_steps, client_id=enrollment.client_id
        )

    with SessionLocal() as session:
        primary = _make_enrollment(
            session,
            campaign_id=campaign_with_steps,
            client_id=client_row,
            is_primary_contact_row=True,
        )
        _make_enrollment(
            session,
            campaign_id=campaign_with_steps,
            client_id=second_client_row,
            is_primary_contact_row=False,
        )
        outcomes = run_due_enrollments(
            session, campaign_id=campaign_with_steps, generate=fake_generate
        )
        session.commit()
        primary_id = primary.enrollment_id

    generated = [o for o in outcomes if o.generated]
    assert len(generated) == 1
    assert generated[0].enrollment_id == primary_id
    assert calls == [primary_id]


def test_run_due_enrollments_records_a_skip_reason_without_generating(
    campaign_with_steps: int, client_row: int
) -> None:
    def fail_generate(session, enrollment, step_no):
        raise AssertionError("generate should not be called for a suppressed client")

    with SessionLocal() as session:
        session.add(Suppression(client_id=client_row, reason="unsubscribe"))
        session.commit()
        _make_enrollment(session, campaign_id=campaign_with_steps, client_id=client_row)
        outcomes = run_due_enrollments(
            session, campaign_id=campaign_with_steps, generate=fail_generate
        )
        session.commit()

    assert len(outcomes) == 1
    assert outcomes[0].generated is False
    assert outcomes[0].reason == "suppressed"


def test_send_touch_advances_the_enrollment_and_audits_the_send(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        message = _make_message(
            session, campaign_id=campaign_with_steps, client_id=client_row, status="approved"
        )
        touch = record_touch(session, enrollment, 1)
        touch.message_id = message.message_id
        session.commit()

        sent = send_touch(session, touch)
        session.commit()
        touch_id = sent.touch_id
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 1
        assert row.status == "in_progress"

        touch_row = session.get(TouchLog, touch_id)
        assert touch_row.sent_at is not None
        assert touch_row.delivery_status == "stubbed"

        audit_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "touch_log",
                    AuditLog.action == "send",
                    AuditLog.entity_id == str(touch_id),
                )
            )
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1


def test_send_touch_refuses_a_message_that_is_not_approved(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        message = _make_message(session, campaign_id=campaign_with_steps, client_id=client_row)
        touch = record_touch(session, enrollment, 1)
        touch.message_id = message.message_id
        session.commit()

        with pytest.raises(ValueError):
            send_touch(session, touch)


def test_send_touch_is_blocked_by_a_suppression_that_arrived_after_approval(
    campaign_with_steps: int, client_row: int
) -> None:
    """Mid-sequence (current_step=1, sending step 2), so the block lands on
    the specific stopped state rather than the before-first-touch excluded
    catch-all, exercising the same state-machine split eligibility does."""
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session,
            campaign_id=campaign_with_steps,
            client_id=client_row,
            current_step=1,
            status="in_progress",
        )
        message = _make_message(
            session, campaign_id=campaign_with_steps, client_id=client_row, status="approved"
        )
        touch = record_touch(session, enrollment, 2)
        touch.message_id = message.message_id
        session.commit()

        session.add(Suppression(client_id=client_row, reason="unsubscribe"))
        session.commit()

        with pytest.raises(SendBlocked):
            send_touch(session, touch)
        session.commit()
        enrollment_id = enrollment.enrollment_id
        touch_id = touch.touch_id

    with SessionLocal() as session:
        assert session.get(TouchLog, touch_id).sent_at is None
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 1
        assert row.status == "stopped_optout"


def test_a_held_angle_generates_but_does_not_send(
    campaign_with_steps: int, client_row: int
) -> None:
    """see_what_changed is held pending the business decision on its exit
    window: resolution, generation, and review all proceed as normal, and
    only the send itself is blocked."""
    with SessionLocal() as session:
        session.add(
            ClientMessageIndicators(
                client_id=client_row,
                message_angle="see_what_changed",
                urgency="low",
                priority_tier="P3",
                prompt_variant="see_what_changed_default",
                rule_name="held_angle_test",
                rule_version=1,
            )
        )
        session.commit()

        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        outcomes = run_due_enrollments(
            session,
            campaign_id=campaign_with_steps,
            generate=lambda s, e, step_no: _make_message(
                s, campaign_id=campaign_with_steps, client_id=client_row
            ),
        )
        session.commit()
        enrollment_id = enrollment.enrollment_id

    assert len(outcomes) == 1
    assert outcomes[0].generated is True

    with SessionLocal() as session:
        touch = session.scalar(
            select(TouchLog).where(TouchLog.enrollment_id == enrollment_id, TouchLog.step_no == 1)
        )
        message = session.get(OutreachMessage, touch.message_id)
        message.status = "approved"
        session.commit()

        with pytest.raises(SendBlocked) as excinfo:
            send_touch(session, touch)
        assert excinfo.value.reason == "angle_held"
        session.commit()
        touch_id = touch.touch_id

    with SessionLocal() as session:
        assert session.get(TouchLog, touch_id).sent_at is None
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 0
        assert row.status == "enrolled"

    with SessionLocal() as session:
        session.execute(
            delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == client_row)
        )
        session.commit()


def test_reconcile_enrollment_catches_current_step_up_after_a_simulated_crash(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        # Simulate send_touch having sent the touch but crashing before it
        # could call advance_enrollment.
        session.add(
            TouchLog(enrollment_id=enrollment.enrollment_id, step_no=1, sent_at=datetime.now(UTC))
        )
        session.commit()

        reconcile_enrollment(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 1
        assert row.status == "in_progress"


def test_reconcile_enrollment_is_a_no_op_when_already_consistent(
    campaign_with_steps: int, client_row: int
) -> None:
    with SessionLocal() as session:
        enrollment = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=client_row
        )
        before = enrollment.next_due_at

        reconcile_enrollment(session, enrollment)
        session.commit()
        enrollment_id = enrollment.enrollment_id

    with SessionLocal() as session:
        row = session.get(Enrollment, enrollment_id)
        assert row.current_step == 0
        assert row.next_due_at == before
