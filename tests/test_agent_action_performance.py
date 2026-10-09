from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from test_api_campaigns import accepted_state, make_settings

from app.agents.action_performance import build_action_performance
from app.agents.results_summary import read_results
from app.agents.tools import get_results
from app.config import get_settings
from app.db.models.action_performance import ActionPerformance
from app.db.models.action_result import ActionResult
from app.db.models.agent_proposal import AgentProposal
from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.durations import parse_hours
from app.llmops.versions import persist_generation_run
from app.privacy.scanners import scan_outbound

FUND_ID = 99630
ACTION = "performance_test_action"
OTHER_ACTION = "performance_test_other_action"
ANGLE = "performance_test_angle"
TIER = "performance_test_tier"
CAMPAIGN_NAME = "Action performance test campaign"
EARLIER_RUN = "action-performance-test-risk-run-1"
LATER_RUN = "action-performance-test-risk-run-2"
CLIENTS = tuple(range(996301, 996309))
A, B, C, D, E, F, G, H = CLIENTS

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
FIRST_WEEK_MON = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
FIRST_WEEK_TUE = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
THIRD_WEEK = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
NOT_YET_COMPLETE = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
WEEK = 168


def _purge(session) -> None:
    session.execute(
        delete(ActionPerformance).where(ActionPerformance.action_code.in_([ACTION, OTHER_ACTION]))
    )
    session.execute(delete(ActionResult).where(ActionResult.client_id.in_(CLIENTS)))
    session.execute(delete(RiskSnapshot).where(RiskSnapshot.client_id.in_(CLIENTS)))
    session.execute(delete(RiskRun).where(RiskRun.run_id.in_([EARLIER_RUN, LATER_RUN])))
    session.execute(delete(AuditLog).where(AuditLog.entity_type == "action_performance"))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(CLIENTS)))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(CLIENTS)))
    session.execute(
        delete(AgentProposal).where(AgentProposal.action_code.in_([ACTION, OTHER_ACTION]))
    )
    session.execute(delete(Campaign).where(Campaign.name == CAMPAIGN_NAME))
    session.execute(delete(Clients).where(Clients.client_id.in_(CLIENTS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.commit()


def _proposal(session, campaign_id: int, action_code: str) -> int:
    proposal = AgentProposal(
        action_code=action_code,
        catalog_version=1,
        group_name="getting_smaller",
        client_count=1,
        evidence="evidence",
        reason="reason",
        angle=ANGLE,
        content_mix="balanced",
        permission_applied="approve_each",
        response_kind="automated_email",
        status="sent",
        campaign_id=campaign_id,
    )
    session.add(proposal)
    session.commit()
    return proposal.proposal_id


def _result(
    session,
    campaign_id: int,
    proposal_id: int,
    client_id: int,
    sent_at: datetime,
    *,
    replied: bool = False,
    opted_out: bool = False,
    edited: bool = False,
    deposit: float = 0.0,
) -> None:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.commit()
    state = accepted_state(client_id)
    state["priority_tier"] = TIER
    generation = persist_generation_run(session, state, make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=generation.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "Subject", "body": "Body"},
        status="approved",
    )
    session.add(message)
    session.commit()
    session.add(
        ActionResult(
            proposal_id=proposal_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            message_id=message.message_id,
            sent_at=sent_at,
            window_days=30,
            replied=replied,
            opted_out=opted_out,
            reviewer_changed=edited,
            deposited=deposit > 0,
            deposit_amount_kes=deposit,
            measured_at=NOW,
        )
    )
    session.commit()


def _snapshot(session, run_id: str, client_id: int, band: str, created_at: datetime) -> None:
    if session.get(RiskRun, run_id) is None:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
    session.add(
        RiskSnapshot(
            run_id=run_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            sig_heavy_withdrawal=False,
            sig_dormant=False,
            sig_broken_pattern=False,
            sig_shrinking=False,
            sig_going_dormant=False,
            sig_never_repeated=False,
            risk_score=10,
            risk_band=band,
            risk_reasons="no signal",
            fund_at_risk=0.0,
            config_version=1,
            created_at=created_at,
        )
    )
    session.commit()


def _build_scenario(session) -> None:
    session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="An action performance test fund"))
    campaign = Campaign(name=CAMPAIGN_NAME, status="running")
    session.add(campaign)
    session.commit()
    main = _proposal(session, campaign.campaign_id, ACTION)
    other = _proposal(session, campaign.campaign_id, OTHER_ACTION)
    campaign_id = campaign.campaign_id

    _result(session, campaign_id, main, A, FIRST_WEEK_MON, replied=True, deposit=5000.0)
    _result(session, campaign_id, main, B, FIRST_WEEK_MON, opted_out=True)
    _result(session, campaign_id, main, C, FIRST_WEEK_TUE, edited=True, deposit=3000.0)
    _result(session, campaign_id, main, D, FIRST_WEEK_TUE)
    _result(session, campaign_id, main, E, THIRD_WEEK, deposit=1000.0)
    _result(session, campaign_id, main, H, NOT_YET_COMPLETE, deposit=700.0)
    _result(session, campaign_id, other, F, FIRST_WEEK_MON)
    _result(session, campaign_id, other, G, FIRST_WEEK_MON)

    _snapshot(session, EARLIER_RUN, A, "High", datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
    _snapshot(session, EARLIER_RUN, C, "High", datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
    _snapshot(session, LATER_RUN, A, "Low", datetime(2026, 8, 20, 3, 0, tzinfo=UTC))
    _snapshot(session, LATER_RUN, E, "Low", datetime(2026, 8, 25, 3, 0, tzinfo=UTC))


@pytest.fixture
def scenario(db: None):
    with SessionLocal() as session:
        _purge(session)
        _build_scenario(session)
    yield
    with SessionLocal() as session:
        _purge(session)


def _use_reading_settings(monkeypatch, lookback: str) -> None:
    monkeypatch.setenv("ACTION_PERFORMANCE_PERIOD", "7d")
    monkeypatch.setenv("ACTION_PERFORMANCE_LOOKBACK", lookback)
    monkeypatch.setenv("ACTION_PERFORMANCE_READ_WINDOW_DAYS", "30")
    monkeypatch.setenv("AGENT_QUERY_MIN_GROUP_SIZE", "5")
    get_settings.cache_clear()


@pytest.fixture
def reading_forever_back(monkeypatch):
    _use_reading_settings(monkeypatch, "36500d")
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def reading_ninety_days_back(monkeypatch):
    _use_reading_settings(monkeypatch, "90d")
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def _rows(session, action_code: str = ACTION) -> list[ActionPerformance]:
    return list(
        session.scalars(
            select(ActionPerformance)
            .where(ActionPerformance.action_code == action_code)
            .order_by(ActionPerformance.period_start, ActionPerformance.risk_band)
        )
    )


def test_each_period_is_added_up_into_counts_and_rates(scenario) -> None:
    with SessionLocal() as session:
        build_action_performance(session, now=NOW, period_hours=WEEK, windows=(30,))
        rows = _rows(session)

    assert len(rows) == 3
    first_high, first_unknown, third_unknown = rows
    assert first_high.period_end - first_high.period_start == timedelta(hours=WEEK)
    assert first_high.period_start == first_unknown.period_start

    assert (first_high.risk_band, first_high.sent_count) == ("High", 2)
    assert (first_high.replied_count, first_high.opted_out_count) == (1, 0)
    assert (first_high.edited_count, first_high.deposited_count) == (1, 2)
    assert first_high.money_in_kes == 8000.0
    assert (first_high.reply_rate, first_high.opt_out_rate) == (0.5, 0.0)
    assert (first_high.edit_rate, first_high.deposit_rate) == (0.5, 1.0)

    assert (first_unknown.risk_band, first_unknown.sent_count) == ("unknown", 2)
    assert first_unknown.opt_out_rate == 0.5
    assert (first_unknown.deposit_rate, first_unknown.money_in_kes) == (0.0, 0.0)

    assert third_unknown.risk_band == "unknown"
    assert (third_unknown.sent_count, third_unknown.money_in_kes) == (1, 1000.0)

    assert {row.priority_tier for row in rows} == {TIER}
    assert {row.angle for row in rows} == {ANGLE}
    assert {row.content_mix for row in rows} == {"balanced"}
    assert {row.variant for row in rows} == {"none"}


def test_an_empty_week_has_no_row_and_a_period_still_open_is_left_out(scenario) -> None:
    with SessionLocal() as session:
        build_action_performance(session, now=NOW, period_hours=WEEK, windows=(30,))
        starts = {row.period_start for row in _rows(session)}
        total_money = sum(row.money_in_kes for row in _rows(session))

    assert len(starts) == 2
    assert total_money == 9000.0

    with SessionLocal() as session:
        build_action_performance(session, now=datetime(2020, 1, 1, tzinfo=UTC), windows=(30,))
        assert _rows(session) == []


def test_the_period_can_be_set_in_hours_and_rebuilding_never_adds_rows(scenario) -> None:
    with SessionLocal() as session:
        build_action_performance(session, now=NOW, period_hours=12, windows=(30,))
        build_action_performance(session, now=NOW, period_hours=12, windows=(30,))
        rows = _rows(session)

    assert {row.period_end - row.period_start for row in rows} == {timedelta(hours=12)}
    assert len(rows) == 5
    first_day = [row for row in rows if row.risk_band == "High"]
    assert len(first_day) == 2
    assert first_day[0].period_start != first_day[1].period_start


def test_the_period_setting_takes_hours_and_days() -> None:
    assert parse_hours("12h", setting="X") == 12
    assert parse_hours("36h", setting="X") == 36
    assert parse_hours("1d", setting="X") == 24
    assert parse_hours(" 7D ", setting="X") == 168
    for bad in ("", "7", "0d", "1.5d", "two days", "-3h"):
        with pytest.raises(ValueError):
            parse_hours(bad, setting="X")


def test_get_results_gives_rates_and_holds_back_a_group_too_small_to_hide_in(
    scenario, reading_forever_back
) -> None:
    with SessionLocal() as session:
        build_action_performance(session, now=NOW, period_hours=WEEK, windows=(30,))
        answer = get_results(session, action_code=ACTION)
        small = get_results(session, action_code=OTHER_ACTION)

    (line,) = answer["results"]
    assert line["action_code"] == ACTION
    assert line["sent_count"] == 5
    assert line["reply_percent"] == 20.0
    assert line["opt_out_percent"] == 20.0
    assert line["edit_percent"] == 20.0
    assert line["deposit_percent"] == 60.0
    assert line["money_in_kes"] == "9,000"
    assert line["periods"] == 2
    scan_outbound(json.dumps(answer, sort_keys=True))

    assert small["results"] == []
    assert small["withheld_small_groups"] == 1
    assert small["message"] == "nothing has been measured for this yet"


def test_results_older_than_the_lookback_are_not_read(scenario, reading_ninety_days_back) -> None:
    with SessionLocal() as session:
        build_action_performance(session, now=NOW, period_hours=WEEK, windows=(30,))
        recent = read_results(session, action_code=ACTION, now=NOW)
        much_later = read_results(session, action_code=ACTION, now=NOW + timedelta(days=200))

    assert [line.sent_count for line in recent.lines] == [5]
    assert much_later.lines == ()
