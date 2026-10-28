from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.agents.campaign_agent import CHOOSE_TOUCH_PLAN_TOOL_NAME, run_campaign_agent
from app.agents.tool_runtime import TOOL_DATA_KEY
from app.agents.tools import PLANNING_TOOLS, make_campaign_read_tools
from app.campaigns.slot_context import (
    DEPOSITED,
    EXCERPT_LIMIT,
    FIRST_TOUCH,
    FOLLOW_UP,
    OPTED_OUT,
    build_campaign_slots,
)
from app.config import Settings
from app.db.models.agent_run import CAMPAIGN_AGENT, AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.campaigns import (
    CampaignStep,
    ContactEvent,
    Enrollment,
    TouchLog,
    TouchPlan,
)
from app.db.models.models import Clients, Funds, PiiVault, Transactions
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.routing import ClientSide
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest
from app.schemas.campaigns import CampaignPlanConfig

_FUND_ID = 9872
_USAGE = LLMUsage(input_tokens=10, output_tokens=10)
_LONG_BODY = "Markets are moving and your plan is waiting for you. " * 12


def _settings() -> Settings:
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


class FakeClient:
    model = "fake-campaign-model"

    def __init__(self, turns: list[ConversationTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def converse(self, *, system, messages, tools=()):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        return self._turns.pop(0)


def _turn(*requests: ToolUseRequest) -> ConversationTurn:
    return ConversationTurn(text="", tool_requests=requests, usage=_USAGE, stop_reason="tool_use")


def _request(call_id: str, name: str, payload: dict) -> ToolUseRequest:
    return ToolUseRequest(call_id=call_id, tool_name=name, tool_input=payload)


def _final() -> ConversationTurn:
    return ConversationTurn(text="Done.", tool_requests=(), usage=_USAGE, stop_reason="end_turn")


def _ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=_FUND_ID, unit_fund_name="Slot Fund"))
        campaign = Campaign(name="slot context campaign", is_test=True)
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
        client_ids = list(
            session.scalars(select(Clients.client_id).where(Clients.unit_fund_id == _FUND_ID))
        )
        enrollment_ids = list(
            session.scalars(
                select(Enrollment.enrollment_id).where(Enrollment.campaign_id == campaign_id)
            )
        )
        session.execute(delete(Transactions).where(Transactions.client_id.in_(client_ids)))
        session.execute(delete(ContactEvent).where(ContactEvent.client_id.in_(client_ids)))
        session.execute(delete(TouchPlan).where(TouchPlan.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.campaign_id == campaign_id))
        run_ids = [
            row[0]
            for row in session.execute(
                text("SELECT run_id FROM generation_runs WHERE client_id = ANY(:ids)"),
                {"ids": client_ids},
            )
        ]
        if run_ids:
            for table in ("llm_requests", "evaluations", "tool_calls", "trace_refs"):
                session.execute(
                    text(f"DELETE FROM {table} WHERE run_id = ANY(:ids)"), {"ids": run_ids}
                )
            session.execute(
                text("DELETE FROM generation_runs WHERE run_id = ANY(:ids)"), {"ids": run_ids}
            )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(ClientSide).where(ClientSide.client_id.in_(client_ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(client_ids)))
        session.execute(delete(Clients).where(Clients.client_id.in_(client_ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND_ID))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type.in_(("touch_plan", "enrollment", "pii_vault"))
            )
        )
        agent_runs = session.query(AgentRun.run_id).filter(AgentRun.agent_kind == CAMPAIGN_AGENT)
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(agent_runs)))
        session.execute(delete(AgentRun).where(AgentRun.agent_kind == CAMPAIGN_AGENT))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def _enroll(session, campaign_id: int, client_id: int, *, opted_out: bool = False, **fields) -> int:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=_FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.add(
        PiiVault(
            client_id=client_id,
            client_name="Slot Client",
            contact_email="slot@example.com",
            opt_out_flag=opted_out,
        )
    )
    session.flush()
    row = Enrollment(campaign_id=campaign_id, client_id=client_id, **fields)
    session.add(row)
    session.commit()
    return row.enrollment_id


def _follow_up(session, campaign_id: int, client_id: int, **fields) -> int:
    return _enroll(
        session,
        campaign_id,
        client_id,
        current_step=1,
        status="in_progress",
        next_due_at=_ago(1),
        enrolled_at=_ago(30),
        **fields,
    )


def _sent(
    session,
    enrollment_id: int,
    client_id: int,
    campaign_id: int,
    *,
    step_no: int = 1,
    days_ago: int = 8,
    angle: str = "back_on_schedule",
    subject: str = "Your plan is waiting",
    body: str = "Short body",
    note: str | None = None,
    **touch_fields,
) -> None:
    run = persist_generation_run(session, _accepted_state(client_id, body), _settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        client_id=client_id,
        ai_draft_content={"subject": subject, "body": body},
        status="approved",
    )
    session.add(message)
    agent_run = AgentRun(trigger="manual", agent_kind=CAMPAIGN_AGENT)
    session.add(agent_run)
    session.flush()
    session.add(
        TouchPlan(
            enrollment_id=enrollment_id,
            step_no=step_no,
            angle=angle,
            gap_days=5,
            reason="r",
            run_id=agent_run.run_id,
            next_time_note=note,
        )
    )
    session.add(
        TouchLog(
            enrollment_id=enrollment_id,
            step_no=step_no,
            message_id=message.message_id,
            sent_at=_ago(days_ago),
            delivery_status=touch_fields.pop("delivery_status", "sent"),
            **touch_fields,
        )
    )
    session.commit()


def _event(session, client_id: int, kind: str, days_ago: int) -> None:
    session.add(ContactEvent(client_id=client_id, type=kind, occurred_at=_ago(days_ago)))
    session.commit()


def _deposit(session, client_id: int, txn_id: int, days_ago: int) -> None:
    session.add(
        Transactions(
            txn_id=txn_id,
            txn_type="purchase",
            client_id=client_id,
            unit_fund_id=_FUND_ID,
            txn_date=_ago(days_ago).date(),
            amount=3000.0,
        )
    )
    session.commit()


def test_a_fresh_enrollment_is_a_first_touch_with_no_history(book: int) -> None:
    with SessionLocal() as session:
        _enroll(session, book, 987201)
        slots = build_campaign_slots(session, book)

    assert slots.dropped == ()
    (slot,) = slots.ready
    assert slot.kind == FIRST_TOUCH
    assert slot.step_no == 1
    assert slot.earlier_touches == ()
    assert slot.prior_angle is None
    assert slot.missing_history is False


def test_a_follow_up_carries_every_earlier_touch_and_what_happened_since(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987202)
        _sent(session, enrollment_id, 987202, book, body=_LONG_BODY, note="ask about the habit")
        _event(session, 987202, "open", 3)
        slots = build_campaign_slots(session, book)

    (slot,) = slots.ready
    assert slot.kind == FOLLOW_UP
    assert slot.step_no == 2
    assert slot.prior_angle == "back_on_schedule"
    (touch,) = slot.earlier_touches
    assert touch.subject == "Your plan is waiting"
    assert len(touch.excerpt) == EXCERPT_LIMIT
    assert touch.send_status == "sent"
    assert touch.delivery_status == "sent"
    assert touch.days_since_sent == 8
    assert (touch.opened, touch.replied, touch.bounced) == (True, False, False)
    assert [(e.type, e.days_ago) for e in slot.events_since_last_touch] == [("open", 3)]
    assert slot.next_time_note == "ask about the habit"


def test_a_follow_up_with_no_touch_on_record_is_flagged_not_dropped(book: int) -> None:
    with SessionLocal() as session:
        _follow_up(session, book, 987203)
        slots = build_campaign_slots(session, book)

    (slot,) = slots.ready
    assert slot.kind == FOLLOW_UP
    assert slot.earlier_touches == ()
    assert slot.missing_history is True


def test_a_bounce_after_the_send_drops_the_slot_and_stops_the_enrollment(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987204)
        _sent(session, enrollment_id, 987204, book)
        _event(session, 987204, "bounce", 7)
        slots = build_campaign_slots(session, book)
        status = session.get(Enrollment, enrollment_id).status

    assert slots.ready == ()
    assert slots.dropped_reasons() == {"bounce": 1}
    assert status == "stopped_bounce"


def test_a_reply_after_the_send_drops_the_slot(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987205)
        _sent(session, enrollment_id, 987205, book)
        _event(session, 987205, "reply", 2)
        slots = build_campaign_slots(session, book)

    assert slots.ready == ()
    assert slots.dropped_reasons() == {"replied": 1}


def test_a_deposit_after_the_send_drops_the_slot_with_a_band_not_an_amount(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987206)
        _sent(session, enrollment_id, 987206, book)
        _deposit(session, 987206, 987206001, days_ago=2)
        slots = build_campaign_slots(session, book)
        status = session.get(Enrollment, enrollment_id).status

    assert slots.ready == ()
    (dropped,) = slots.dropped
    assert dropped.skip_reason == DEPOSITED
    assert dropped.deposit_since_last_touch == "1000 to 5250"
    assert status == "stopped_reengaged"


def test_a_deposit_from_before_the_last_send_is_not_counted(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987207)
        _sent(session, enrollment_id, 987207, book)
        _deposit(session, 987207, 987207001, days_ago=20)
        slots = build_campaign_slots(session, book)

    assert len(slots.ready) == 1
    assert slots.dropped == ()


def test_an_opted_out_client_is_dropped(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987208, opted_out=True)
        _sent(session, enrollment_id, 987208, book)
        slots = build_campaign_slots(session, book)

    assert slots.ready == ()
    assert slots.dropped_reasons() == {"opted_out": 1}


def test_an_opt_out_event_alone_drops_the_slot_and_stops_the_enrollment(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987209)
        _sent(session, enrollment_id, 987209, book)
        _event(session, 987209, "optout", 4)
        slots = build_campaign_slots(session, book)
        status = session.get(Enrollment, enrollment_id).status

    assert slots.dropped_reasons() == {OPTED_OUT: 1}
    assert status == "stopped_optout"


def test_the_tools_show_days_and_bands_and_never_ids_or_dates(book: int) -> None:
    with SessionLocal() as session:
        enrollment_id = _follow_up(session, book, 987210)
        _sent(
            session,
            enrollment_id,
            987210,
            book,
            side_at_send="inactive",
            balance_at_send=100.0,
        )
        session.add(
            ClientSide(
                client_id=987210,
                side="inactive",
                balance=700.0,
                threshold_kes=6000.0,
                source_feed="inactive",
            )
        )
        session.commit()
        slots = build_campaign_slots(session, book)

    tools = make_campaign_read_tools(
        plan_config=CampaignPlanConfig(objective="win_back_deposit"),
        touch_points=3,
        allowed_angles={"back_on_schedule"},
        slots=slots.ready,
    )
    history = tools["get_touch_history"](None, slot=0)
    side = tools["get_side_change"](None, slot=0)

    assert history["touches"][0]["days_since_sent"] == 8
    assert side["side_now"] == "inactive"
    assert side["balance_band_now"] == "600 to 6000"
    assert side["at_send"] == [
        {"step_no": 1, "side": "inactive", "balance_band": "50 to 600", "balance_since": "up"}
    ]
    rendered = json.dumps([history, side])
    for leaked in ("enrollment_id", "client_id", "987210", str(datetime.now(UTC).year)):
        assert leaked not in rendered
    assert tools["get_touch_history"](None, slot=5)["error"] == "unknown_slot"
    assert tools["get_campaign_context"](None)["touch_points"] == 3


def test_the_planner_only_sees_slots_that_can_still_be_planned(book: int) -> None:
    with SessionLocal() as session:
        _enroll(session, book, 987211)
        replied = _follow_up(session, book, 987212)
        _sent(session, replied, 987212, book)
        _event(session, 987212, "reply", 2)
        gone = _follow_up(session, book, 987213, opted_out=True)
        _sent(session, gone, 987213, book)
        ready = _follow_up(session, book, 987214)
        _sent(session, ready, 987214, book, note="ask about the weekly habit")

    plan = {
        "decisions": [
            {
                "slot": 0,
                "angle": "back_on_schedule",
                "gap_days": 4,
                "reason": "first touch",
                "next_time": "  check if\nthey opened  ",
            },
            {"slot": 1, "angle": "not_a_goodbye", "gap_days": 6, "reason": "follow up"},
        ]
    }
    client = FakeClient(
        [
            _turn(_request("history", "get_touch_history", {"slot": 1})),
            _turn(_request("choose", CHOOSE_TOUCH_PLAN_TOOL_NAME, plan)),
            _final(),
        ]
    )

    with SessionLocal() as session:
        run = run_campaign_agent(session, book, llm_client=client)
        notes = list(
            session.scalars(select(TouchPlan.next_time_note).where(TouchPlan.run_id == run.run_id))
        )

    assert run.state == "completed"
    assert run.summary == "planned 2 of 2 due touches; left out 2 (1 opted_out, 1 replied)"
    system = client.calls[0]["system"]
    assert "slot 0: first touch" in system
    assert "slot 1: follow up, touch 2 last angle back_on_schedule 8 days" in system
    assert "note from the last plan: ask about the weekly habit" in system
    assert "slot 2" not in system
    assert {spec.name for spec in client.calls[0]["tools"]} == {
        *PLANNING_TOOLS,
        CHOOSE_TOUCH_PLAN_TOOL_NAME,
    }
    answer = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert list(json.loads(answer)) == [TOOL_DATA_KEY]
    assert sorted(note for note in notes if note) == ["check if they opened"]
