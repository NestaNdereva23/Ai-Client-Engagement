from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from test_api_campaigns import accepted_state, make_settings

from app.agents.action_results import (
    SentTouch,
    _balance_up,
    _moved_to_active,
    measure_action_results,
)
from app.agents.tools import get_campaign_results
from app.campaigns import results as results_module
from app.campaigns.explain import explain_campaign
from app.campaigns.results import NO_DATA, TOO_FEW, campaign_results
from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.agent_run import CAMPAIGN_AGENT, AgentRun, AgentToolCall
from app.db.models.audit import AuditLog
from app.db.models.campaigns import (
    CampaignStep,
    ContactEvent,
    Enrollment,
    TouchLog,
    TouchPlan,
)
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.routing import ClientSide
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app

FUND_ID = 99650
DEPOSITOR = 996501
LATE = 996502
QUIET = 996503
ALL_CLIENTS = (DEPOSITOR, LATE, QUIET)
CAMPAIGN_NAME = "Campaign results test campaign"
WINDOW = 20

SENT_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
NOW = SENT_AT + timedelta(days=60)
LOADED_AT = NOW + timedelta(days=2)


def _purge(session) -> None:
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.client_id.in_(ALL_CLIENTS))
    ).all() or [0]
    run_ids = session.scalars(
        select(TouchPlan.run_id).where(TouchPlan.enrollment_id.in_(enrollment_ids))
    ).all() or [0]
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id.in_(ALL_CLIENTS))
    ).all() or [""]
    campaign_ids = session.scalars(
        select(Campaign.campaign_id).where(Campaign.name == CAMPAIGN_NAME)
    ).all() or [0]
    session.execute(delete(ActionResult).where(ActionResult.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(AuditLog).where(AuditLog.entity_type == "action_result"))
    session.execute(delete(ContactEvent).where(ContactEvent.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ActiveTransaction).where(ActiveTransaction.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ClientSide).where(ClientSide.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
    session.execute(delete(TouchPlan).where(TouchPlan.enrollment_id.in_(enrollment_ids)))
    session.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
    session.execute(delete(AgentRun).where(AgentRun.run_id.in_(run_ids)))
    session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Enrollment).where(Enrollment.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(CampaignStep).where(CampaignStep.campaign_id.in_(campaign_ids)))
    session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.commit()


def _send(session, *, campaign_id: int, run_id: int, client_id: int, value_band: str) -> None:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.commit()
    session.add(ClientFeatures(client_id=client_id, value_band=value_band))
    enrollment = Enrollment(
        campaign_id=campaign_id, client_id=client_id, current_step=1, status="in_progress"
    )
    session.add(enrollment)
    generation = persist_generation_run(session, accepted_state(client_id), make_settings())
    generation.priority_tier = "hot_leads"
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=generation.run_id,
        client_id=client_id,
        ai_draft_content={"subject": f"Subject {client_id}", "body": "Body"},
        status="approved",
    )
    session.add(message)
    session.commit()
    session.add_all(
        [
            TouchPlan(
                enrollment_id=enrollment.enrollment_id,
                step_no=1,
                angle="winback_habit",
                gap_days=5,
                reason=f"reason {client_id}",
                run_id=run_id,
                cited_facts=[
                    {
                        "text": "A fact",
                        "url": "https://example.com/rate",
                        "retrieved_at": "2026-07-30T08:00:00+00:00",
                        "tool": "search_web",
                    }
                ],
            ),
            TouchLog(
                enrollment_id=enrollment.enrollment_id,
                step_no=1,
                message_id=message.message_id,
                sent_at=SENT_AT,
                delivery_status="sent",
                side_at_send="inactive",
                balance_at_send=100.0,
            ),
            ReviewAction(
                message_id=message.message_id, reviewer_id="reviewer-1", outcome="approve"
            ),
            ClientSide(
                client_id=client_id,
                side="active" if client_id == DEPOSITOR else "inactive",
                balance=5000.0 if client_id == DEPOSITOR else 100.0,
                threshold_kes=6000.0,
                source_feed="test",
            ),
        ]
    )
    session.commit()


def _build(session) -> None:
    session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="A campaign results test fund"))
    campaign = Campaign(
        name=CAMPAIGN_NAME,
        status="running",
        planning_mode="agent",
        agent_touch_policy={"observation_days": WINDOW},
    )
    run = AgentRun(trigger="manual", agent_kind=CAMPAIGN_AGENT, state="completed")
    session.add_all([campaign, run])
    session.commit()
    session.add(
        AgentToolCall(
            run_id=run.run_id,
            ordinal=1,
            tool_name="get_campaign_results",
            tool_input={},
            tool_output=dict(NO_DATA),
        )
    )
    for client_id, band in ((DEPOSITOR, "High"), (LATE, "Low"), (QUIET, "Low")):
        _send(
            session,
            campaign_id=campaign.campaign_id,
            run_id=run.run_id,
            client_id=client_id,
            value_band=band,
        )
    session.add(
        ActiveClientFund(
            client_id=DEPOSITOR,
            unit_fund_id=FUND_ID,
            n_deposits=1,
            n_withdrawals=0,
            updated_at=LOADED_AT,
        )
    )
    session.add_all(
        ActiveTransaction(
            txn_id=txn_id,
            txn_type="purchase",
            client_id=client_id,
            unit_fund_id=FUND_ID,
            txn_date=txn_date,
            amount=amount,
        )
        for txn_id, client_id, txn_date, amount in (
            (99650001, DEPOSITOR, date(2026, 8, 4), 5000.0),
            (99650002, LATE, date(2026, 8, 26), 3000.0),
        )
    )
    session.add(
        ContactEvent(client_id=DEPOSITOR, type="open", occurred_at=SENT_AT + timedelta(days=1))
    )
    session.commit()


@pytest.fixture
def scenario(db: None):
    with SessionLocal() as session:
        _purge(session)
        _build(session)
        campaign_id = session.scalar(
            select(Campaign.campaign_id).where(Campaign.name == CAMPAIGN_NAME)
        )
    yield campaign_id
    with SessionLocal() as session:
        _purge(session)


def _measure(now: datetime = NOW) -> None:
    with SessionLocal() as session:
        measure_action_results(session, now=now, windows=())


def _touches(campaign_id: int) -> dict[int, dict]:
    with SessionLocal() as session:
        explained = explain_campaign(session, campaign_id)
    return {touch["client_id"]: touch for touch in explained["touches"]}


def test_a_touch_can_be_followed_from_plan_to_message_to_send_to_deposit(scenario) -> None:
    _measure()

    with SessionLocal() as session:
        explained = explain_campaign(session, scenario)
    touch = {t["client_id"]: t for t in explained["touches"]}[DEPOSITOR]

    assert touch["stage"] == "measured"
    assert touch["angle"] == "winback_habit"
    assert touch["plan"]["reason"] == f"reason {DEPOSITOR}"
    assert touch["plan"]["cited_facts"][0]["url"] == "https://example.com/rate"
    assert touch["draft"]["subject"] == f"Subject {DEPOSITOR}"
    assert touch["approval"]["outcome"] == "approve"
    assert touch["send"]["delivery_status"] == "sent"
    outcome = touch["outcome"]
    assert (outcome["window_days"], outcome["deposit_seen"]) == (WINDOW, True)
    assert outcome["deposit_amount_kes"] == 5000.0
    assert outcome["opened"] is True
    assert (outcome["moved_to_active"], outcome["balance_up"]) == (True, True)


def test_the_earlier_results_the_agent_looked_at_come_with_the_plan(scenario) -> None:
    with SessionLocal() as session:
        explained = explain_campaign(session, scenario)

    run_id = explained["touches"][0]["plan"]["run_id"]
    [run] = explained["runs"]
    assert run["run_id"] == run_id
    assert run["results_seen"][0]["results"]["status"] == "not_enough_data"


def test_a_touch_with_no_deposit_is_measured_as_no_deposit(scenario) -> None:
    _measure()

    outcome = _touches(scenario)[QUIET]["outcome"]

    assert (outcome["deposit_seen"], outcome["deposit_amount_kes"]) == (False, 0.0)
    assert (outcome["opened"], outcome["replied"], outcome["bounced"]) == (False, False, False)
    assert (outcome["moved_to_active"], outcome["balance_up"]) == (False, False)


def test_a_deposit_after_the_campaigns_window_is_not_counted(scenario) -> None:
    _measure()

    outcome = _touches(scenario)[LATE]["outcome"]

    assert outcome["window_days"] == WINDOW
    assert outcome["deposit_seen"] is False


def test_a_touch_whose_window_is_still_open_is_not_measured(scenario) -> None:
    _measure(now=SENT_AT + timedelta(days=WINDOW - 1))

    touches = _touches(scenario)

    assert {touch["stage"] for touch in touches.values()} == {"sent"}
    assert all(touch["outcome"] is None for touch in touches.values())


def test_measuring_again_does_not_add_rows(scenario) -> None:
    _measure()
    _measure()

    with SessionLocal() as session:
        total = session.scalar(
            select(func.count())
            .select_from(ActionResult)
            .where(ActionResult.client_id.in_(ALL_CLIENTS))
        )

    assert total == len(ALL_CLIENTS)


def test_small_groups_are_marked_too_few_to_tell(scenario) -> None:
    _measure()

    with SessionLocal() as session:
        results = campaign_results(session)

    groups = [
        group
        for cut in ("campaign", "angle", "tier", "value_band")
        for group in results[f"by_{cut}"]
    ]
    assert results["status"] == "ok"
    assert {group["result"] for group in groups} == {TOO_FEW}
    assert all("deposit_seen" not in group for group in groups)


def test_a_group_big_enough_shows_what_was_seen_after_the_email(scenario, monkeypatch) -> None:
    monkeypatch.setattr(results_module, "MIN_GROUP_SIZE", 2)
    _measure()

    with SessionLocal() as session:
        results = get_campaign_results(session)

    [campaign] = results["by_campaign"]
    bands = {group["label"]: group for group in results["by_value_band"]}
    assert campaign["label"] == CAMPAIGN_NAME
    assert (campaign["sent"], campaign["window_days"]) == (3, WINDOW)
    assert campaign["deposit_seen"] == {"count": 1, "of": 3}
    assert campaign["opened"] == {"count": 1, "of": 3}
    assert campaign["moved_to_active"] == {"count": 1, "of": 3}
    assert campaign["balance_up"] == {"count": 1, "of": 3}
    assert bands["High"]["result"] == TOO_FEW
    assert bands["Low"]["deposit_seen"] == {"count": 0, "of": 2}
    assert "not proof" in results["note"]


def test_with_nothing_measured_the_rollup_says_there_is_not_enough_data(scenario) -> None:
    with SessionLocal() as session:
        assert campaign_results(session) == NO_DATA


def test_the_side_flags_stay_blank_when_nothing_was_saved_at_send() -> None:
    touch = SentTouch(1, "m", 1, SENT_AT, WINDOW, None, None)
    today = ClientSide(
        client_id=1, side="active", balance=10.0, threshold_kes=1.0, source_feed="test"
    )

    assert (_moved_to_active(touch, today), _balance_up(touch, today)) == (None, None)
    assert _moved_to_active(replace(touch, side_at_send="inactive"), None) is None


def test_the_explain_endpoint_returns_the_whole_chain_for_a_campaign(
    scenario, configured_reviewers, reviewer_1_headers
) -> None:
    _measure()

    response = TestClient(app).get(
        f"/api/v1/campaigns/{scenario}/explain", headers=reviewer_1_headers
    )

    body = response.json()
    touch = {t["client_id"]: t for t in body["touches"]}[DEPOSITOR]
    assert response.status_code == 200
    assert (touch["stage"], touch["outcome"]["deposit_seen"]) == ("measured", True)
    assert touch["approval"]["outcome"] == "approve"
    assert body["runs"][0]["results_seen"][0]["results"]["status"] == "not_enough_data"


def test_the_explain_endpoint_says_not_found_for_a_campaign_that_does_not_exist(
    db: None, configured_reviewers, reviewer_1_headers
) -> None:
    response = TestClient(app).get(
        "/api/v1/campaigns/987654321/explain", headers=reviewer_1_headers
    )

    assert response.status_code == 404
