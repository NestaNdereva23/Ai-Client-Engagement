from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from test_api_campaigns import accepted_state, make_settings

from app.agents import action_results
from app.agents.action_results import measure_action_results
from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run

FUND_ID = 99620
DEPOSITOR = 996201
QUIET = 996202
OPTED_OUT = 996203
ALL_CLIENTS = (DEPOSITOR, QUIET, OPTED_OUT)
ACTION = "action_result_test_action"
CAMPAIGN_NAME = "Action result test campaign"
TITLE = "Action result test finding"

SENT_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
NOW = SENT_AT + timedelta(days=40)
LOADED_AT = NOW + timedelta(days=2)
WINDOWS = (7, 30)


def _purge(session) -> None:
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id.in_(ALL_CLIENTS))
    ).all()
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.client_id.in_(ALL_CLIENTS))
    ).all()
    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id).where(AgentProposal.action_code == ACTION)
    ).all()
    campaign_ids = session.scalars(
        select(Campaign.campaign_id).where(Campaign.name == CAMPAIGN_NAME)
    ).all()
    session.execute(delete(ActionResult).where(ActionResult.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(AuditLog).where(AuditLog.entity_type == "action_result"))
    session.execute(delete(ContactEvent).where(ContactEvent.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ActiveTransaction).where(ActiveTransaction.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids or [0])))
    session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids or [""])))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Enrollment).where(Enrollment.client_id.in_(ALL_CLIENTS)))
    session.execute(
        delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids or [0]))
    )
    session.execute(delete(AgentProposal).where(AgentProposal.action_code == ACTION))
    session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids or [0])))
    session.execute(delete(AgentInsight).where(AgentInsight.title == TITLE))
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.commit()


def _send_message(session, campaign_id: int, client_id: int) -> str:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.commit()
    enrollment = Enrollment(campaign_id=campaign_id, client_id=client_id)
    session.add(enrollment)
    session.commit()
    generation = persist_generation_run(session, accepted_state(client_id), make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=generation.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "Subject", "body": "Body"},
        personalized_content={"subject": "Subject", "body": "Body"},
        status="approved",
    )
    session.add(message)
    session.commit()
    session.add(
        TouchLog(
            enrollment_id=enrollment.enrollment_id,
            step_no=1,
            message_id=message.message_id,
            sent_at=SENT_AT,
            delivery_status="sent",
        )
    )
    session.commit()
    return message.message_id


def _build(session) -> dict[str, int]:
    session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="An action result test fund"))
    insight = AgentInsight(
        kind="risk",
        title=TITLE,
        group_name="getting_smaller",
        client_count=len(ALL_CLIENTS),
        confidence="high",
        confidence_reason="counted twice",
        suggestion="tell them",
        why_now="the balance keeps falling",
        state="accepted",
    )
    campaign = Campaign(name=CAMPAIGN_NAME, status="running")
    session.add_all([insight, campaign])
    session.commit()

    message_ids = {
        client_id: _send_message(session, campaign.campaign_id, client_id)
        for client_id in ALL_CLIENTS
    }

    proposal = AgentProposal(
        insight_id=insight.insight_id,
        action_code=ACTION,
        catalog_version=1,
        group_name="getting_smaller",
        client_count=len(ALL_CLIENTS),
        evidence="evidence",
        reason="reason",
        permission_applied="approve_each",
        response_kind="automated_email",
        status="sent",
        campaign_id=campaign.campaign_id,
    )
    session.add(proposal)
    session.commit()
    session.add_all(
        AgentProposalClient(
            proposal_id=proposal.proposal_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            included=True,
        )
        for client_id in ALL_CLIENTS
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
            client_id=DEPOSITOR,
            unit_fund_id=FUND_ID,
            txn_date=txn_date,
            amount=amount,
        )
        for txn_id, txn_date, amount in (
            (99620001, date(2026, 8, 4), 5000.0),
            (99620002, date(2026, 8, 21), 2000.0),
        )
    )
    session.add(
        ReviewAction(
            message_id=message_ids[DEPOSITOR], reviewer_id="reviewer-1", outcome="edit_approve"
        )
    )
    session.add_all(
        [
            ContactEvent(client_id=OPTED_OUT, type="open", occurred_at=SENT_AT + timedelta(days=1)),
            ContactEvent(
                client_id=OPTED_OUT, type="optout", occurred_at=SENT_AT + timedelta(days=2)
            ),
        ]
    )
    session.commit()
    return {"proposal_id": proposal.proposal_id, "insight_id": insight.insight_id}


@pytest.fixture
def scenario(db: None):
    with SessionLocal() as session:
        _purge(session)
        ids = _build(session)
    yield ids
    with SessionLocal() as session:
        _purge(session)


def _rows(session) -> dict[tuple[int, int], ActionResult]:
    results = session.scalars(select(ActionResult).where(ActionResult.client_id.in_(ALL_CLIENTS)))
    return {(row.client_id, row.window_days): row for row in results}


def test_a_depositor_a_quiet_client_and_an_opt_out_are_each_measured(scenario) -> None:
    with SessionLocal() as session:
        measure_action_results(session, now=NOW, windows=WINDOWS)
        rows = _rows(session)

    assert len(rows) == len(ALL_CLIENTS) * len(WINDOWS)

    week = rows[(DEPOSITOR, 7)]
    month = rows[(DEPOSITOR, 30)]
    assert (week.deposited, week.deposit_amount_kes) == (True, 5000.0)
    assert (month.deposited, month.deposit_amount_kes) == (True, 7000.0)
    assert week.reviewer_changed is True
    assert week.proposal_id == scenario["proposal_id"]
    assert week.insight_id == scenario["insight_id"]
    assert week.unit_fund_id == FUND_ID

    quiet = rows[(QUIET, 30)]
    assert (quiet.deposited, quiet.deposit_amount_kes) == (False, 0.0)
    assert (quiet.opened, quiet.replied, quiet.opted_out, quiet.reviewer_changed) == (
        False,
        False,
        False,
        False,
    )

    gone = rows[(OPTED_OUT, 7)]
    assert (gone.opened, gone.opted_out, gone.deposited) == (True, True, False)


def test_running_again_for_the_same_window_never_adds_rows(scenario) -> None:
    with SessionLocal() as session:
        measure_action_results(session, now=NOW, windows=WINDOWS)
        second = measure_action_results(session, now=NOW, windows=WINDOWS)
        assert [outcome.measured for outcome in second] == [0, 0]

        later = NOW + timedelta(days=1)
        measure_action_results(session, now=later, windows=WINDOWS, remeasure=True)
        total = session.scalar(
            select(func.count())
            .select_from(ActionResult)
            .where(ActionResult.client_id.in_(ALL_CLIENTS))
        )
        measured_ats = {row.measured_at for row in _rows(session).values()}

    assert total == len(ALL_CLIENTS) * len(WINDOWS)
    assert measured_ats == {later}


def test_a_window_waits_until_deposit_data_has_been_loaded_past_its_end(
    scenario, monkeypatch
) -> None:
    monkeypatch.setattr(
        action_results, "_latest_active_load", lambda session: SENT_AT + timedelta(days=5)
    )

    with SessionLocal() as session:
        outcomes = measure_action_results(session, now=NOW, windows=WINDOWS)
        total = session.scalar(
            select(func.count())
            .select_from(ActionResult)
            .where(ActionResult.client_id.in_(ALL_CLIENTS))
        )

    assert [outcome.measured for outcome in outcomes] == [0, 0]
    assert total == 0
