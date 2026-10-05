from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from test_api_campaigns import accepted_state, make_settings

from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal
from app.db.models.agent_run import AgentRun
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.complaints import ClientComplaint
from app.db.models.llmops import GenerationRun, LLMRequest, LLMResponse
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.signals import ClientSignalState, SignalRun
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.services import agent_metrics

FUND_A = 94400
FUND_B = 94401
RISK_DEP = 944001
RISK_QUIET = 944002
OPP_MULTI = 944003
OPP_SINGLE = 944004
COV_HIT = 944020
COV_MISS_NONE = 944021
COV_MISS_LATE = 944022
AR_CLIENTS = (RISK_DEP, RISK_QUIET, OPP_MULTI, OPP_SINGLE)
COV_CLIENTS = (COV_HIT, COV_MISS_NONE, COV_MISS_LATE)
ALL_CLIENTS = AR_CLIENTS + COV_CLIENTS

CAMPAIGN_NAME = "Metrics test campaign"
ACTION_RISK = "metrics_test_risk_action"
ACTION_OPP = "metrics_test_opp_action"
TITLE_PREFIX = "Metrics test finding"
SIGNAL_RUN_ID = "metrics-test-signal-run"

FOUND = datetime(2031, 1, 6, 12, 0, tzinfo=UTC)
INSIGHT_AT = datetime(2031, 1, 5, 12, 0, tzinfo=UTC)
WINDOW_START = date(2031, 1, 1)
WINDOW_END = date(2031, 3, 31)
WINDOW_DAYS = 30


def _window():
    return agent_metrics.resolve_window(WINDOW_START, WINDOW_END, WINDOW_DAYS)


def _purge(session) -> None:
    gen_ids = session.scalars(
        select(GenerationRun.run_id).where(GenerationRun.client_id.in_(ALL_CLIENTS))
    ).all()
    request_ids = session.scalars(
        select(LLMRequest.request_id).where(LLMRequest.run_id.in_(gen_ids or [""]))
    ).all()
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id.in_(ALL_CLIENTS))
    ).all()
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.client_id.in_(ALL_CLIENTS))
    ).all()
    session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids or [0])))
    session.execute(delete(LLMRequest).where(LLMRequest.request_id.in_(request_ids or [0])))
    session.execute(delete(ActionResult).where(ActionResult.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ContactEvent).where(ContactEvent.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ClientComplaint).where(ClientComplaint.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids or [""])))
    session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids or [0])))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Enrollment).where(Enrollment.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(ALL_CLIENTS)))
    session.execute(
        delete(AgentProposal).where(AgentProposal.action_code.in_((ACTION_RISK, ACTION_OPP)))
    )
    session.execute(delete(AgentInsight).where(AgentInsight.title.like(f"{TITLE_PREFIX}%")))
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(SignalRun).where(SignalRun.run_id == SIGNAL_RUN_ID))
    session.execute(
        delete(AgentRun).where(
            AgentRun.started_at >= datetime(2031, 1, 1, tzinfo=UTC),
            AgentRun.started_at < datetime(2031, 4, 1, tzinfo=UTC),
        )
    )
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id.in_((FUND_A, FUND_B))))
    session.commit()


def _insight(session, *, kind, state, title, dismissed_reason=None) -> AgentInsight:
    insight = AgentInsight(
        kind=kind,
        title=title,
        group_name="getting_smaller",
        client_count=1,
        confidence="high",
        confidence_reason="counted it",
        suggestion="reach out",
        why_now="the moment is now",
        state=state,
        dismissed_reason=dismissed_reason,
        created_at=INSIGHT_AT,
    )
    session.add(insight)
    session.flush()
    return insight


def _proposal(session, *, action_code, permission) -> AgentProposal:
    proposal = AgentProposal(
        action_code=action_code,
        catalog_version=1,
        group_name="getting_smaller",
        client_count=1,
        evidence="the balance keeps falling",
        reason="worth a nudge",
        permission_applied=permission,
        status="sent",
        created_at=datetime(2031, 1, 6, 12, 0, tzinfo=UTC),
    )
    session.add(proposal)
    session.flush()
    return proposal


def _sent_message(session, campaign_id, client_id, sent_at) -> str:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_A, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.flush()
    enrollment = Enrollment(campaign_id=campaign_id, client_id=client_id)
    session.add(enrollment)
    session.flush()
    generation = persist_generation_run(session, accepted_state(client_id), make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=generation.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "s", "body": "b"},
        personalized_content={"subject": "s", "body": "b"},
        status="approved",
    )
    session.add(message)
    session.flush()
    session.add(
        TouchLog(
            enrollment_id=enrollment.enrollment_id,
            step_no=1,
            message_id=message.message_id,
            sent_at=sent_at,
            delivery_status="sent",
            cost=2,
        )
    )
    session.flush()
    return message.message_id


def _action_result(
    session, *, client_id, fund_id, message_id, proposal_id, insight_id, sent_at, deposited, amount
) -> None:
    session.add(
        ActionResult(
            proposal_id=proposal_id,
            insight_id=insight_id,
            client_id=client_id,
            unit_fund_id=fund_id,
            message_id=message_id,
            sent_at=sent_at,
            window_days=WINDOW_DAYS,
            deposited=deposited,
            deposit_amount_kes=amount,
            measured_at=datetime(2031, 2, 20, 12, 0, tzinfo=UTC),
        )
    )


def _fund(session, client_id, fund_id, balance) -> None:
    session.add(
        ActiveClientFund(
            client_id=client_id,
            unit_fund_id=fund_id,
            balance=balance,
            n_deposits=1,
            n_withdrawals=0,
        )
    )


def _run(session, *, kind, trigger, started, seconds, cost) -> None:
    session.add(
        AgentRun(
            started_at=started,
            finished_at=started + timedelta(seconds=seconds),
            state="completed",
            trigger=trigger,
            agent_kind=kind,
            cost_kes=cost,
        )
    )


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)

        session.add_all(
            [
                Funds(unit_fund_id=FUND_A, unit_fund_name="Metrics fund A"),
                Funds(unit_fund_id=FUND_B, unit_fund_name="Metrics fund B"),
            ]
        )
        campaign = Campaign(name=CAMPAIGN_NAME, status="running")
        session.add(campaign)
        session.flush()

        risk = _insight(session, kind="risk", state="accepted", title=f"{TITLE_PREFIX} risk")
        opp = _insight(session, kind="opportunity", state="acted_on", title=f"{TITLE_PREFIX} opp")
        _insight(
            session,
            kind="risk",
            state="dismissed",
            title=f"{TITLE_PREFIX} d1",
            dismissed_reason="duplicate",
        )
        _insight(
            session,
            kind="opportunity",
            state="dismissed",
            title=f"{TITLE_PREFIX} d2",
            dismissed_reason="not relevant",
        )
        _insight(session, kind="risk", state="new", title=f"{TITLE_PREFIX} new")

        p_risk = _proposal(session, action_code=ACTION_RISK, permission="suggest_only")
        p_opp = _proposal(session, action_code=ACTION_OPP, permission="act_alone")

        m1 = _sent_message(
            session, campaign.campaign_id, RISK_DEP, datetime(2031, 1, 5, 12, tzinfo=UTC)
        )
        m2 = _sent_message(
            session, campaign.campaign_id, RISK_QUIET, datetime(2031, 1, 7, 12, tzinfo=UTC)
        )
        m3 = _sent_message(
            session, campaign.campaign_id, OPP_MULTI, datetime(2031, 1, 10, 12, tzinfo=UTC)
        )
        m4 = _sent_message(
            session, campaign.campaign_id, OPP_SINGLE, datetime(2031, 1, 20, 12, tzinfo=UTC)
        )
        _sent_message(session, campaign.campaign_id, COV_HIT, datetime(2031, 1, 8, 12, tzinfo=UTC))
        _sent_message(
            session, campaign.campaign_id, COV_MISS_LATE, datetime(2031, 1, 20, 12, tzinfo=UTC)
        )

        _action_result(
            session,
            client_id=RISK_DEP,
            fund_id=FUND_A,
            message_id=m1,
            proposal_id=p_risk.proposal_id,
            insight_id=risk.insight_id,
            sent_at=datetime(2031, 1, 5, 12, tzinfo=UTC),
            deposited=True,
            amount=5000,
        )
        _action_result(
            session,
            client_id=RISK_QUIET,
            fund_id=FUND_A,
            message_id=m2,
            proposal_id=p_risk.proposal_id,
            insight_id=risk.insight_id,
            sent_at=datetime(2031, 1, 7, 12, tzinfo=UTC),
            deposited=False,
            amount=0,
        )
        _action_result(
            session,
            client_id=OPP_MULTI,
            fund_id=FUND_A,
            message_id=m3,
            proposal_id=p_opp.proposal_id,
            insight_id=opp.insight_id,
            sent_at=datetime(2031, 1, 10, 12, tzinfo=UTC),
            deposited=True,
            amount=3000,
        )
        _action_result(
            session,
            client_id=OPP_SINGLE,
            fund_id=FUND_A,
            message_id=m4,
            proposal_id=p_opp.proposal_id,
            insight_id=opp.insight_id,
            sent_at=datetime(2031, 1, 20, 12, tzinfo=UTC),
            deposited=True,
            amount=2000,
        )

        _fund(session, RISK_DEP, FUND_A, 8000)
        _fund(session, RISK_QUIET, FUND_A, 500)
        _fund(session, OPP_MULTI, FUND_A, 1500)
        _fund(session, OPP_MULTI, FUND_B, 1000)
        _fund(session, OPP_SINGLE, FUND_A, 2000)

        session.add_all(
            [
                ReviewAction(
                    message_id=m1,
                    reviewer_id="r@x",
                    outcome="edit_approve",
                    created_at=datetime(2031, 1, 6, 12, tzinfo=UTC),
                ),
                ReviewAction(
                    message_id=m2,
                    reviewer_id="r@x",
                    outcome="reject",
                    created_at=datetime(2031, 1, 6, 12, tzinfo=UTC),
                ),
                ReviewAction(
                    message_id=m3,
                    reviewer_id="r@x",
                    outcome="approve",
                    created_at=datetime(2031, 1, 6, 12, tzinfo=UTC),
                ),
            ]
        )
        session.add(
            ContactEvent(
                client_id=RISK_QUIET,
                type="optout",
                occurred_at=datetime(2031, 1, 7, 12, tzinfo=UTC),
            )
        )
        session.add(
            ClientComplaint(
                client_id=RISK_DEP,
                opened_at=date(2031, 1, 6),
                status="open",
                category="service",
                channel="email",
            )
        )

        signal_run = SignalRun(run_id=SIGNAL_RUN_ID, state="completed")
        session.add(signal_run)
        session.flush()
        for client_id in COV_CLIENTS:
            session.add(
                ClientSignalState(
                    client_id=client_id,
                    unit_fund_id=FUND_A,
                    signal_code="small_balance",
                    is_active=True,
                    since=date(2031, 1, 6),
                    run_id=SIGNAL_RUN_ID,
                )
            )

        _run(
            session,
            kind="intelligence",
            trigger="manual",
            started=datetime(2031, 1, 10, 10, tzinfo=UTC),
            seconds=120,
            cost=200,
        )
        _run(
            session,
            kind="nightly",
            trigger="nightly",
            started=datetime(2031, 1, 11, 10, tzinfo=UTC),
            seconds=300,
            cost=100,
        )
        _run(
            session,
            kind="action",
            trigger="manual",
            started=datetime(2031, 1, 12, 10, tzinfo=UTC),
            seconds=30,
            cost=50,
        )

        gen = session.scalar(select(GenerationRun).where(GenerationRun.client_id == RISK_DEP))
        for attempt, latency in ((1, 100), (2, 300)):
            request = LLMRequest(
                run_id=gen.run_id,
                attempt=attempt,
                model_version_id=gen.model_version_id,
                system_prompt="x",
            )
            session.add(request)
            session.flush()
            session.add(
                LLMResponse(
                    request_id=request.request_id,
                    latency_ms=latency,
                    created_at=datetime(2031, 1, 10, 12, tzinfo=UTC),
                )
            )

        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def test_speed_counts_days_from_found_to_contacted(book):
    with SessionLocal() as session:
        out = agent_metrics.speed_metric(session, _window())
    assert out.sample == 4
    assert out.average_days == pytest.approx(5.5)
    assert out.median_days == pytest.approx(3.5)
    assert out.p90_days == pytest.approx(12.0)
    assert out.buckets.same_day == 1
    assert out.buckets.within_three_days == 1
    assert out.buckets.within_a_week == 1
    assert out.buckets.over_a_week == 1


def test_coverage_is_contacted_within_a_week_over_found(book):
    with SessionLocal() as session:
        out = agent_metrics.coverage_metric(session, _window())
    assert out.clients_found == 3
    assert out.contacted_within_a_week == 1
    assert out.coverage_rate == pytest.approx(1 / 3)


def test_money_at_risk_sums_balance_and_deposits(book):
    with SessionLocal() as session:
        out = agent_metrics.money_at_risk_metric(session, _window())
    assert out.clients_reached == 2
    assert out.money_reached_kes == pytest.approx(8500)
    assert out.deposited_clients == 1
    assert out.deposited_money_kes == pytest.approx(5000)


def test_losses_prevented_counts_those_kept_above_the_floor(book):
    with SessionLocal() as session:
        out = agent_metrics.losses_prevented_metric(session, _window())
    assert out.at_risk_contacted == 2
    assert out.stayed_clients == 1
    assert out.money_kept_kes == pytest.approx(5000)


def test_growth_found_needs_a_second_fund(book):
    with SessionLocal() as session:
        out = agent_metrics.growth_found_metric(session, _window())
    assert out.introduced_contacted == 2
    assert out.took_second_fund == 1
    assert out.money_followed_kes == pytest.approx(3000)


def test_quality_totals_across_weeks(book):
    with SessionLocal() as session:
        out = agent_metrics.quality_metric(session, _window())
    assert sum(week.decisions for week in out.weeks) == 3
    assert sum(week.edits for week in out.weeks) == 1
    assert sum(week.rejections for week in out.weeks) == 1
    assert sum(week.sent for week in out.weeks) == 6
    assert sum(week.opt_outs for week in out.weeks) == 1
    assert sum(week.complaints for week in out.weeks) == 1


def test_usefulness_counts_states_and_reasons(book):
    with SessionLocal() as session:
        out = agent_metrics.usefulness_metric(session, _window())
    assert out.total == 5
    assert out.accepted == 1
    assert out.acted_on == 1
    assert out.dismissed == 2
    assert out.new == 1
    assert out.acceptance_rate == pytest.approx(0.5)
    assert [(r.reason, r.count) for r in out.dismiss_reasons] == [
        ("duplicate", 1),
        ("not relevant", 1),
    ]


def test_freedom_counts_each_level(book):
    with SessionLocal() as session:
        out = agent_metrics.freedom_metric(session, _window())
    assert sum(week.total for week in out.weeks) == 2
    assert sum(week.suggest_only for week in out.weeks) == 1
    assert sum(week.act_alone for week in out.weeks) == 1


def test_cost_per_finding_and_per_contacted_and_return(book):
    with SessionLocal() as session:
        out = agent_metrics.cost_metric(session, _window())
    assert out.finding_count == 5
    assert out.discovery_cost_kes == pytest.approx(300)
    assert out.cost_per_finding_kes == pytest.approx(60)
    assert out.contacted_clients == 4
    assert out.action_cost_kes == pytest.approx(50)
    assert out.send_spend_kes == pytest.approx(12)
    assert out.cost_per_contacted_kes == pytest.approx(15.5)
    assert out.money_in_kes == pytest.approx(10000)
    assert out.total_cost_kes == pytest.approx(362)
    assert out.return_multiple == pytest.approx(10000 / 362)


def test_system_speed_runs_latency_and_book(book):
    with SessionLocal() as session:
        out = agent_metrics.system_speed_metric(session, _window())
    by_kind = {run.agent_kind: run for run in out.runs}
    assert by_kind["intelligence"].median_seconds == pytest.approx(120)
    assert by_kind["nightly"].median_seconds == pytest.approx(300)
    assert by_kind["action"].median_seconds == pytest.approx(30)
    assert out.model_latency.sample == 2
    assert out.model_latency.p50_ms == pytest.approx(200)
    assert out.model_latency.p90_ms == pytest.approx(280)
    assert out.book.active_client_funds >= 5
    assert out.book.active_clients >= 4


def test_dashboard_bundles_every_number(book):
    with SessionLocal() as session:
        out = agent_metrics.dashboard(session, _window())
    assert out.speed.sample == 4
    assert out.coverage.clients_found == 3
    assert out.money_at_risk.clients_reached == 2
    assert out.cost.finding_count == 5
    assert out.system_speed.model_latency.sample == 2


def test_every_number_has_its_own_endpoint():
    from app.main import app

    paths = set(app.openapi().get("paths", {}))
    for suffix in (
        "",
        "/speed",
        "/coverage",
        "/money-at-risk",
        "/losses-prevented",
        "/growth",
        "/quality",
        "/usefulness",
        "/freedom",
        "/cost",
        "/system-speed",
    ):
        assert f"/api/v1/agent/metrics{suffix}" in paths
