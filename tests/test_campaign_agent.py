from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

import app.campaigns.generation as generation_module
from app.agents.campaign_agent import (
    CHOOSE_TOUCH_PLAN_TOOL_NAME,
    CampaignPlanFailed,
    _require_saved_plans,
    make_choose_touch_plan_tool,
    run_campaign_agent,
)
from app.agents.orchestrator import Orchestrator
from app.campaigns.generation import _planned_orchestrator, _repeat_prohibition, _touch_plan_for
from app.campaigns.scheduler import advance_enrollment
from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.campaign_agent_run import PLAN_RUN, CampaignAgentRun, CampaignAgentToolCall
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest


def _make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


def _accepted_state(client_id: int, body: str) -> dict:
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
        "raw_structured_output": {"subject": "Come back", "body": body},
    }


def _make_message(session, *, campaign_id: int, client_id: int, body: str) -> OutreachMessage:
    run = persist_generation_run(session, _accepted_state(client_id, body), _make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "Come back", "body": body},
    )
    session.add(message)
    session.flush()
    return message


_FUND_ID = 9781
_CLIENT_A = 978101
_CLIENT_B = 978102
_USAGE = LLMUsage(input_tokens=10, output_tokens=10)


class FakeConversingLLMClient:
    model = "fake-campaign-model"

    def __init__(self, turns: list[ConversationTurn]) -> None:
        self._turns = list(turns)

    def converse(self, *, system, messages, tools=()):
        if not self._turns:
            raise AssertionError("the fake model was asked for more turns than scripted")
        return self._turns.pop(0)


def _final_answer(text: str = "Done.") -> ConversationTurn:
    return ConversationTurn(text=text, tool_requests=(), usage=_USAGE, stop_reason="end_turn")


def _choose_touch_plan_turn(decisions: list[dict]) -> ConversationTurn:
    return ConversationTurn(
        text="",
        tool_requests=(
            ToolUseRequest(
                call_id="choose_1",
                tool_name=CHOOSE_TOUCH_PLAN_TOOL_NAME,
                tool_input={"decisions": decisions},
            ),
        ),
        usage=_USAGE,
        stop_reason="tool_use",
    )


@pytest.fixture
def fund_and_clients(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=_FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        for client_id in (_CLIENT_A, _CLIENT_B):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=_FUND_ID,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()

    yield

    with SessionLocal() as session:
        run_ids = [
            row[0]
            for row in session.execute(
                text("SELECT run_id FROM generation_runs WHERE client_id IN (:a, :b)"),
                {"a": _CLIENT_A, "b": _CLIENT_B},
            )
        ]
        if run_ids:
            session.execute(
                text("DELETE FROM outreach_message WHERE generation_run_id = ANY(:ids)"),
                {"ids": run_ids},
            )
            for table in ("llm_requests", "evaluations", "tool_calls", "trace_refs"):
                session.execute(
                    text(f"DELETE FROM {table} WHERE run_id = ANY(:ids)"), {"ids": run_ids}
                )
            session.execute(
                text("DELETE FROM generation_runs WHERE run_id = ANY(:ids)"), {"ids": run_ids}
            )
        session.execute(delete(Clients).where(Clients.unit_fund_id == _FUND_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND_ID))
        session.commit()


@pytest.fixture
def campaign_with_steps(fund_and_clients: None):
    with SessionLocal() as session:
        campaign = Campaign(name="test campaign agent campaign", is_test=True)
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        for step_no in (1, 2, 3):
            session.add(
                CampaignStep(campaign_id=campaign_id, step_no=step_no, offset_days=step_no * 7)
            )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_type == "touch_plan"))
        enrollment_ids = session.query(Enrollment.enrollment_id).filter(
            Enrollment.campaign_id == campaign_id
        )
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(TouchPlan).where(TouchPlan.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.campaign_id == campaign_id))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        run_ids = session.query(CampaignAgentRun.run_id).filter(CampaignAgentRun.kind == PLAN_RUN)
        session.execute(
            delete(CampaignAgentToolCall).where(CampaignAgentToolCall.run_id.in_(run_ids))
        )
        session.execute(delete(CampaignAgentRun).where(CampaignAgentRun.kind == PLAN_RUN))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def _make_enrollment(session, *, campaign_id: int, client_id: int, **overrides) -> int:
    row = Enrollment(campaign_id=campaign_id, client_id=client_id, **overrides)
    session.add(row)
    session.commit()
    return row.enrollment_id


def _make_run(session) -> int:
    run = CampaignAgentRun(trigger="manual", kind=PLAN_RUN, state="completed")
    session.add(run)
    session.commit()
    return run.run_id


def test_choose_touch_plan_writes_a_row_and_an_audit_entry(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        run_id = _make_run(session)
        slots = [{"slot": 0, "enrollment_id": enrollment_id, "step_no": 1, "prior_angle": None}]
        outcome: dict = {}
        tool = make_choose_touch_plan_tool(
            slots=slots,
            allowed_angles={"winback_habit", "winback_value"},
            min_gap_days=2,
            max_gap_days=10,
            run_id=run_id,
            outcome=outcome,
        )
        result = tool(
            session,
            decisions=[
                {
                    "slot": 0,
                    "angle": "winback_habit",
                    "gap_days": 5,
                    "reason": "first touch",
                    "next_time": "check in",
                }
            ],
        )
        session.commit()

        assert result == {"status": "written", "count": 1}
        assert outcome["written"] == 1
        row = session.query(TouchPlan).filter_by(enrollment_id=enrollment_id, step_no=1).one()
        assert row.angle == "winback_habit"
        assert row.gap_days == 5
        audit = (
            session.query(AuditLog)
            .filter_by(entity_type="touch_plan", entity_id=str(row.plan_id))
            .one()
        )
        assert audit.detail["angle"] == "winback_habit"


def test_choose_touch_plan_refuses_the_clients_own_last_angle(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        run_id = _make_run(session)
        slots = [
            {
                "slot": 0,
                "enrollment_id": enrollment_id,
                "step_no": 2,
                "prior_angle": "winback_habit",
            }
        ]
        tool = make_choose_touch_plan_tool(
            slots=slots,
            allowed_angles={"winback_habit", "winback_value"},
            min_gap_days=2,
            max_gap_days=10,
            run_id=run_id,
            outcome={},
        )
        result = tool(
            session,
            decisions=[
                {
                    "slot": 0,
                    "angle": "winback_habit",
                    "gap_days": 5,
                    "reason": "same as before",
                    "next_time": "check in",
                }
            ],
        )
        assert result["error"] == "repeats_last_angle"
        assert session.query(TouchPlan).filter_by(enrollment_id=enrollment_id).count() == 0


def test_choose_touch_plan_refuses_a_gap_outside_the_campaign_bounds(
    campaign_with_steps: int,
) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        run_id = _make_run(session)
        slots = [{"slot": 0, "enrollment_id": enrollment_id, "step_no": 1, "prior_angle": None}]
        tool = make_choose_touch_plan_tool(
            slots=slots,
            allowed_angles={"winback_habit"},
            min_gap_days=2,
            max_gap_days=10,
            run_id=run_id,
            outcome={},
        )
        result = tool(
            session,
            decisions=[
                {
                    "slot": 0,
                    "angle": "winback_habit",
                    "gap_days": 30,
                    "reason": "too far out",
                    "next_time": "check in",
                }
            ],
        )
        assert result["error"] == "gap_out_of_range"


def test_choose_touch_plan_refuses_an_incomplete_batch(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_a = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        enrollment_b = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_B
        )
        run_id = _make_run(session)
        slots = [
            {"slot": 0, "enrollment_id": enrollment_a, "step_no": 1, "prior_angle": None},
            {"slot": 1, "enrollment_id": enrollment_b, "step_no": 1, "prior_angle": None},
        ]
        tool = make_choose_touch_plan_tool(
            slots=slots,
            allowed_angles={"winback_habit"},
            min_gap_days=2,
            max_gap_days=10,
            run_id=run_id,
            outcome={},
        )
        result = tool(
            session,
            decisions=[
                {
                    "slot": 0,
                    "angle": "winback_habit",
                    "gap_days": 5,
                    "reason": "only one of two",
                    "next_time": "check in",
                }
            ],
        )
        assert result["error"] == "incomplete"


def test_advance_enrollment_uses_the_planned_gap_instead_of_the_step_offset(
    campaign_with_steps: int,
) -> None:
    with SessionLocal() as session:
        enrollment = session.get(
            Enrollment,
            _make_enrollment(session, campaign_id=campaign_with_steps, client_id=_CLIENT_A),
        )
        sent_at = datetime(2026, 11, 1, tzinfo=UTC)
        advance_enrollment(session, enrollment, step_no=1, sent_at=sent_at, gap_days=3)
        assert enrollment.next_due_at == datetime(2026, 11, 4, tzinfo=UTC)


def test_planned_orchestrator_passes_the_angle_override_and_registers_under_email(
    campaign_with_steps: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    class FakeAgent:
        channel = "email"

    def fake_build_default_agent(session, settings, **kwargs):
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr(generation_module, "build_default_agent", fake_build_default_agent)

    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        plan = TouchPlan(
            enrollment_id=enrollment_id,
            step_no=1,
            angle="winback_value",
            gap_days=4,
            reason="varying it up",
            run_id=_make_run(session),
        )
        session.add(plan)
        session.commit()

        orchestrator = _planned_orchestrator(
            session,
            plan=plan,
            enrollment_id=enrollment_id,
            client_id=_CLIENT_A,
            settings=Settings(llm_provider="anthropic", anthropic_api_key="test-key"),
            tracer=None,
        )

    assert isinstance(orchestrator, Orchestrator)
    assert captured["angle_override"] == "winback_value"


def test_repeat_prohibition_reads_the_previous_steps_own_draft(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        message = _make_message(
            session,
            campaign_id=campaign_with_steps,
            client_id=_CLIENT_A,
            body="We miss you at Cytonn.",
        )
        session.add(TouchLog(enrollment_id=enrollment_id, step_no=1, message_id=message.message_id))
        session.commit()

        prohibition = _repeat_prohibition(session, enrollment_id, 2)
        assert prohibition is not None
        assert "We miss you at Cytonn." in prohibition

        assert _repeat_prohibition(session, enrollment_id, 1) is None


def test_touch_plan_for_finds_the_row_for_this_step_only(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        session.add(
            TouchPlan(
                enrollment_id=enrollment_id,
                step_no=1,
                angle="winback_habit",
                gap_days=5,
                reason="r",
                run_id=_make_run(session),
            )
        )
        session.commit()

        assert _touch_plan_for(session, enrollment_id, 1).angle == "winback_habit"
        assert _touch_plan_for(session, enrollment_id, 2) is None


def test_run_campaign_agent_plans_every_due_enrollment_in_one_call(
    campaign_with_steps: int,
) -> None:
    with SessionLocal() as session:
        enrollment_a = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        enrollment_b = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_B
        )

    fake_client = FakeConversingLLMClient(
        [
            _choose_touch_plan_turn(
                [
                    {
                        "slot": 0,
                        "angle": "back_on_schedule",
                        "gap_days": 4,
                        "reason": "first touch for this client",
                        "next_time": "check in",
                    },
                    {
                        "slot": 1,
                        "angle": "not_a_goodbye",
                        "gap_days": 6,
                        "reason": "first touch for this client",
                        "next_time": "check in",
                    },
                ]
            ),
            _final_answer(),
        ]
    )

    with SessionLocal() as session:
        run = run_campaign_agent(session, campaign_with_steps, llm_client=fake_client)
        assert run.state == "completed"
        plans = {
            row.enrollment_id: row.angle
            for row in session.query(TouchPlan).filter(
                TouchPlan.enrollment_id.in_([enrollment_a, enrollment_b])
            )
        }
        assert plans == {enrollment_a: "back_on_schedule", enrollment_b: "not_a_goodbye"}


def test_the_touch_plan_endpoint_returns_the_latest_runs_decisions(
    campaign_with_steps: int, configured_reviewers, reviewer_1_headers: dict[str, str]
) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        session.add(
            TouchPlan(
                enrollment_id=enrollment_id,
                step_no=1,
                angle="back_on_schedule",
                gap_days=4,
                reason="first touch for this client",
                run_id=_make_run(session),
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get(
        f"/api/v1/campaigns/{campaign_with_steps}/touch-plan", headers=reviewer_1_headers
    )
    assert response.status_code == 200
    assert response.json() == [
        {
            "enrollment_id": enrollment_id,
            "client_id": _CLIENT_A,
            "step_no": 1,
            "prior_angle": None,
            "angle": "back_on_schedule",
            "gap_days": 4,
            "reason": "first touch for this client",
            "next_time_note": None,
            "cited_facts": None,
        }
    ]


_OPEN_ANGLES = {"winback_habit", "winback_value", "not_a_goodbye"}
_GOOD = {
    "slot": 0,
    "angle": "winback_value",
    "gap_days": 5,
    "reason": "a new approach",
    "next_time": "see if it was opened",
}
_REPEAT = {**_GOOD, "angle": "winback_habit"}


def _step_two_tool(session, enrollment_id: int, outcome: dict, run_id: int):
    slots = [
        {
            "slot": 0,
            "enrollment_id": enrollment_id,
            "step_no": 2,
            "prior_angle": "winback_habit",
            "earlier_angles": ["winback_habit"],
            "earlier_subjects": [],
        }
    ]
    return make_choose_touch_plan_tool(
        slots=slots,
        allowed_angles=_OPEN_ANGLES,
        min_gap_days=2,
        max_gap_days=10,
        run_id=run_id,
        outcome=outcome,
    )


def _plan_count(session, enrollment_id: int) -> int:
    return session.query(TouchPlan).filter_by(enrollment_id=enrollment_id).count()


def test_a_refusal_lists_every_problem_and_counts_down_the_tries(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        outcome: dict = {}
        tool = _step_two_tool(session, enrollment_id, outcome, _make_run(session))
        bad = {"slot": 0, "angle": "winback_habit", "gap_days": 99, "reason": ""}

        tries = [tool(session, decisions=[bad]) for _ in range(3)]

    assert [t["tries_left"] for t in tries] == [2, 1, 0]
    assert [e["code"] for e in tries[0]["errors"]] == [
        "repeats_last_angle",
        "gap_out_of_range",
        "missing_reason",
        "missing_next_time",
    ]
    assert tries[0]["error"] == "repeats_last_angle"
    assert outcome["failed_tries"] == 3


def test_a_call_after_the_last_try_saves_nothing_even_if_it_is_valid(
    campaign_with_steps: int,
) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        outcome: dict = {}
        tool = _step_two_tool(session, enrollment_id, outcome, _make_run(session))
        for _ in range(3):
            tool(session, decisions=[_REPEAT])

        result = tool(session, decisions=[_GOOD])

        assert result["error"] == "no_tries_left"
        assert _plan_count(session, enrollment_id) == 0
        assert "written" not in outcome


def test_a_valid_retry_after_a_refusal_saves_the_plan(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        outcome: dict = {}
        tool = _step_two_tool(session, enrollment_id, outcome, _make_run(session))

        refused = tool(session, decisions=[_REPEAT])
        saved = tool(session, decisions=[_GOOD])

        assert refused["error"] == "repeats_last_angle"
        assert saved == {"status": "written", "count": 1}
        assert outcome["failed_tries"] == 1
        assert _plan_count(session, enrollment_id) == 1


def test_a_second_save_in_the_same_run_is_refused(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        tool = _step_two_tool(session, enrollment_id, {}, _make_run(session))
        tool(session, decisions=[_GOOD])

        again = tool(session, decisions=[_GOOD])

        assert again["error"] == "already_saved"
        assert _plan_count(session, enrollment_id) == 1


def test_a_malformed_call_is_a_refusal_not_a_crash(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        tool = _step_two_tool(session, enrollment_id, {}, _make_run(session))

        assert tool(session, decisions="all of them")["error"] == "invalid_decisions"
        assert tool(session, decisions=["slot 0"])["error"] == "invalid_decisions"


def test_a_duplicate_plan_is_a_clear_error_and_leaves_the_other_slots_unsaved(
    campaign_with_steps: int,
) -> None:
    with SessionLocal() as session:
        enrollment_a = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        enrollment_b = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_B
        )
        run_id = _make_run(session)
        session.add(
            TouchPlan(
                enrollment_id=enrollment_b,
                step_no=2,
                angle="not_a_goodbye",
                gap_days=3,
                reason="earlier run",
                run_id=run_id,
            )
        )
        session.commit()
        slots = [
            {"slot": n, "enrollment_id": e, "step_no": 2, "prior_angle": None}
            for n, e in enumerate((enrollment_a, enrollment_b))
        ]
        outcome: dict = {}
        tool = make_choose_touch_plan_tool(
            slots=slots,
            allowed_angles=_OPEN_ANGLES,
            min_gap_days=2,
            max_gap_days=10,
            run_id=run_id,
            outcome=outcome,
        )

        result = tool(session, decisions=[_GOOD, {**_GOOD, "slot": 1}])

        assert result["error"] == "duplicate_plan"
        assert _plan_count(session, enrollment_a) == 0
        assert _plan_count(session, enrollment_b) == 1
        assert session.query(AuditLog).filter_by(entity_type="touch_plan").count() == 0
        assert "written" not in outcome


def _search_call(session, run_id: int, snippet: str) -> None:
    session.add(
        CampaignAgentToolCall(
            run_id=run_id,
            ordinal=1,
            tool_name="search_web",
            tool_input={"query": "redeem"},
            tool_output={
                "query": "redeem",
                "retrieved_at": "2026-10-28T10:00:00+00:00",
                "results": [{"title": "t", "url": "https://example.com/a", "snippet": snippet}],
            },
        )
    )
    session.commit()


def test_a_cited_fact_is_saved_with_its_source_and_time(campaign_with_steps: int) -> None:
    snippet = "Money market funds can usually be redeemed within one working day."
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        run_id = _make_run(session)
        _search_call(session, run_id, snippet)
        tool = _step_two_tool(session, enrollment_id, {}, run_id)

        result = tool(session, decisions=[{**_GOOD, "research_notes": [snippet]}])

        assert result["status"] == "written"
        row = session.query(TouchPlan).filter_by(enrollment_id=enrollment_id).one()
        assert row.cited_facts == [
            {
                "text": snippet,
                "url": "https://example.com/a",
                "retrieved_at": "2026-10-28T10:00:00+00:00",
                "tool": "search_web",
            }
        ]
        assert row.next_time_note == "see if it was opened"


def test_a_fact_the_tools_never_returned_saves_nothing(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _make_enrollment(
            session, campaign_id=campaign_with_steps, client_id=_CLIENT_A
        )
        run_id = _make_run(session)
        _search_call(session, run_id, "Funds can be redeemed within one working day.")
        tool = _step_two_tool(session, enrollment_id, {}, run_id)

        result = tool(
            session,
            decisions=[{**_GOOD, "research_notes": ["Every fund doubles in a year."]}],
        )

        assert result["error"] == "unsupported_fact"
        assert _plan_count(session, enrollment_id) == 0


def test_the_saved_count_must_match_the_due_slots(campaign_with_steps: int) -> None:
    with SessionLocal() as session:
        run_id = _make_run(session)

        with pytest.raises(CampaignPlanFailed, match="saved 0 plans for 2 due touches"):
            _require_saved_plans(session, run_id, 2, {"written": 2}, "final_answer")
        with pytest.raises(CampaignPlanFailed, match="never saved a plan"):
            _require_saved_plans(session, run_id, 1, {}, "final_answer")
        with pytest.raises(CampaignPlanFailed, match="ran out of turns"):
            _require_saved_plans(session, run_id, 1, {}, "turn_limit")


def test_a_failed_planning_run_answers_502_with_the_reasons(
    campaign_with_steps: int, configured_reviewers, reviewer_1_headers, monkeypatch
) -> None:
    def fail(*args, **kwargs):
        raise CampaignPlanFailed("plans refused after 3 tries: slot 0 needs a next_time note")

    monkeypatch.setattr("app.api.routers.campaigns.plan_and_run_due_enrollments", fail)
    monkeypatch.setattr(
        "app.api.routers.campaigns.build_default_orchestrator", lambda *a, **k: None
    )

    response = TestClient(app).post(
        f"/api/v1/campaigns/{campaign_with_steps}/plan-and-generate", headers=reviewer_1_headers
    )

    assert response.status_code == 502
    assert "next_time" in response.text
