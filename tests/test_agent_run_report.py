from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_api_campaigns import accepted_state, make_settings

from app.agents import run_report_email
from app.agents.permissions import set_permission
from app.agents.run_report import build_run_report
from app.agents.run_report_email import announce_run_finished, render_run_report, send_run_report
from app.config import get_settings
from app.db.models.agent_event import PAUSED, AgentEvent
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_permission import AgentPermission
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.agent_run import AgentRun
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.delivery.mailer import NullMailer
from app.llmops.versions import persist_generation_run
from app.main import app

FUND_ID = 99331
SENT_CLIENTS = (993301, 993302, 993303, 993304)
STOPPED_CLIENTS = (993305, 993306, 993307)
ALL_CLIENTS = SENT_CLIENTS + STOPPED_CLIENTS
ACTION = "report_test_action"
MARK = "report test run"
TITLE_PREFIX = "Report test finding"
CAMPAIGN_NAME = "Report test campaign"

REPORT_URL = "/api/v1/agent/runs/{run_id}/report"

client = TestClient(app)


def _purge(session) -> None:
    campaign_ids = session.scalars(
        select(Campaign.campaign_id).where(Campaign.name == CAMPAIGN_NAME)
    ).all()
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id.in_(ALL_CLIENTS))
    ).all()
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.client_id.in_(ALL_CLIENTS))
    ).all()
    session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids or [0])))
    session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids or [""])))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Enrollment).where(Enrollment.client_id.in_(ALL_CLIENTS)))

    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id).where(AgentProposal.action_code == ACTION)
    ).all()
    session.execute(
        delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids or [0]))
    )
    session.execute(delete(AgentProposal).where(AgentProposal.action_code == ACTION))
    session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids or [0])))
    session.execute(delete(AgentInsight).where(AgentInsight.title.like(f"{TITLE_PREFIX}%")))

    run_ids = session.scalars(select(AgentRun.run_id).where(AgentRun.summary == MARK)).all()
    session.execute(delete(AuditLog).where(AuditLog.run_id.in_([str(i) for i in run_ids or [0]])))
    session.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids or [0])))
    session.execute(delete(AgentRun).where(AgentRun.run_id.in_(run_ids or [0])))

    permission_ids = session.scalars(
        select(AgentPermission.permission_id).where(AgentPermission.action_code == ACTION)
    ).all()
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "agent_permission",
            AuditLog.entity_id.in_([str(i) for i in permission_ids or [0]]),
        )
    )
    session.execute(delete(AgentPermission).where(AgentPermission.action_code == ACTION))
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_CLIENTS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.commit()


@pytest.fixture(autouse=True)
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _run(session, *, cost_kes: float | None = None) -> AgentRun:
    started = datetime(2026, 9, 30, 11, 10, 4, tzinfo=UTC)
    run = AgentRun(
        trigger="manual",
        agent_kind="intelligence",
        state="completed",
        started_at=started,
        finished_at=started + timedelta(seconds=94),
        summary=MARK,
        cost_kes=cost_kes,
    )
    session.add(run)
    session.commit()
    return run


def _insight(session, run_id: int, state: str, **extra) -> AgentInsight:
    row = AgentInsight(
        run_id=run_id,
        kind="risk",
        title=f"{TITLE_PREFIX} {state}",
        group_name="getting_smaller",
        client_count=5,
        confidence="high",
        confidence_reason="counted twice",
        suggestion="tell them",
        why_now="the balance keeps falling",
        state=state,
        **extra,
    )
    session.add(row)
    session.commit()
    return row


def _campaign_with_messages(session, client_ids, *, statuses, sent_count: int) -> int:
    if session.get(Funds, FUND_ID) is None:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="A report test fund"))
        session.commit()
    campaign = Campaign(name=CAMPAIGN_NAME, status="running")
    session.add(campaign)
    session.commit()
    first_message_id = None
    for index, (client_id, status) in enumerate(zip(client_ids, statuses, strict=True)):
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        enrollment = Enrollment(campaign_id=campaign.campaign_id, client_id=client_id)
        session.add(enrollment)
        session.commit()
        generation = persist_generation_run(session, accepted_state(client_id), make_settings())
        message = OutreachMessage(
            message_id=uuid4().hex,
            campaign_id=campaign.campaign_id,
            generation_run_id=generation.run_id,
            client_id=client_id,
            ai_draft_content={"subject": "Subject", "body": "Body"},
            personalized_content={"subject": "Subject", "body": "Body"},
            status=status,
        )
        session.add(message)
        session.commit()
        first_message_id = first_message_id or message.message_id
        if index < sent_count:
            session.add(
                TouchLog(
                    enrollment_id=enrollment.enrollment_id,
                    step_no=1,
                    message_id=message.message_id,
                    sent_at=datetime.now(UTC),
                    delivery_status="sent",
                )
            )
            session.commit()
    session.add(
        ReviewAction(message_id=first_message_id, reviewer_id="reviewer-1", outcome="edit_approve")
    )
    session.commit()
    return campaign.campaign_id


def _proposal(session, *, insight_id, run_id, campaign_id, status, included, skipped) -> None:
    proposal = AgentProposal(
        run_id=run_id,
        insight_id=insight_id,
        action_code=ACTION,
        catalog_version=1,
        group_name="getting_smaller",
        client_count=len(included) + len(skipped),
        evidence="evidence",
        reason="reason",
        permission_applied="approve_each",
        content_mix="mostly_learning",
        response_kind="automated_email",
        status=status,
        campaign_id=campaign_id,
    )
    session.add(proposal)
    session.commit()
    rows = [
        AgentProposalClient(
            proposal_id=proposal.proposal_id, client_id=cid, unit_fund_id=FUND_ID, included=True
        )
        for cid in included
    ] + [
        AgentProposalClient(
            proposal_id=proposal.proposal_id,
            client_id=cid,
            unit_fund_id=FUND_ID,
            included=False,
            skip_reason=reason,
        )
        for cid, reason in skipped
    ]
    session.add_all(rows)
    session.commit()


def test_a_run_with_sends_reports_every_number_from_the_records() -> None:
    with SessionLocal() as session:
        run = _run(session, cost_kes=21.4)
        accepted = _insight(session, run.run_id, "accepted")
        _insight(
            session,
            run.run_id,
            "dismissed",
            dismissed_reason="growth offers are not switched on yet",
            decided_by="reviewer-1",
            decided_at=datetime.now(UTC),
        )
        _insight(session, run.run_id, "new")
        campaign_id = _campaign_with_messages(
            session,
            SENT_CLIENTS,
            statuses=("approved", "approved", "pending_review", "pending_review"),
            sent_count=2,
        )
        _proposal(
            session,
            insight_id=accepted.insight_id,
            run_id=None,
            campaign_id=campaign_id,
            status="running",
            included=SENT_CLIENTS,
            skipped=((993390, "contacted_recently"), (993391, "open_complaint")),
        )
        set_permission(session, ACTION, "approve_each", changed_by="lead", changed_reason="on")
        set_permission(
            session,
            ACTION,
            "approve_sample",
            changed_by="lead",
            changed_reason="34 approved, 3 edited",
        )
        session.commit()
        run_id = run.run_id

        report = build_run_report(session, run)

    totals = report.totals
    assert (totals.found, totals.accepted, totals.dismissed, totals.undecided) == (3, 1, 1, 1)
    assert (totals.drafted, totals.sent, totals.left_out) == (4, 2, 2)
    assert report.waiting.drafts_in_review == 2
    assert report.waiting.new_findings == 1
    assert report.cost.agent_kes == 21.4
    assert report.run.duration_seconds == 94

    action = report.actions[0]
    assert action.skip_reasons == {"contacted_recently": 1, "open_complaint": 1}
    assert (action.messages.approved, action.messages.edited) == (2, 1)
    assert action.content_mix == "mostly_learning"
    assert [(row.content_mix, row.drafted, row.sent) for row in report.mixes] == [
        ("mostly_learning", 4, 2)
    ]
    assert {row.reason: row.count for row in report.skipped} == action.skip_reasons

    level = next(row for row in report.levels if row.action_code == ACTION)
    assert level.level == "approve_sample"
    assert [change.to_level for change in level.history] == ["approve_each", "approve_sample"]
    assert level.history[1].from_level == "approve_each"
    assert level.history[1].reason == "34 approved, 3 edited"

    body = render_run_report(report).text_body
    assert "Sent 2" in body
    assert "Mostly guiding: 1 action, drafted 4, sent 2" in body
    assert "growth offers are not switched on yet" in body

    response = client.get(REPORT_URL.format(run_id=run_id))
    assert response.status_code == 200
    assert response.json()["totals"]["sent"] == 2


def test_a_run_that_found_nothing_says_so_and_unknown_runs_are_404() -> None:
    with SessionLocal() as session:
        run = _run(session)
        run_id = run.run_id

    response = client.get(REPORT_URL.format(run_id=run_id))
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["found"] == 0
    assert body["totals"]["sent"] == 0
    assert body["actions"] == []
    assert body["skipped"] == []
    assert body["waiting"]["drafts_in_review"] == 0
    assert body["waiting"]["new_findings"] == 0

    assert client.get(REPORT_URL.format(run_id=999999999)).status_code == 404


def test_a_stopped_proposal_shows_what_was_sent_and_what_never_went() -> None:
    with SessionLocal() as session:
        run = _run(session)
        session.add(AgentEvent(run_id=run.run_id, ordinal=1, kind=PAUSED, detail={}))
        campaign_id = _campaign_with_messages(
            session,
            STOPPED_CLIENTS,
            statuses=("approved", "pending_review", "pending_review"),
            sent_count=1,
        )
        _proposal(
            session,
            insight_id=None,
            run_id=run.run_id,
            campaign_id=campaign_id,
            status="stopped",
            included=STOPPED_CLIENTS,
            skipped=(),
        )
        report = build_run_report(session, run)

    action = report.actions[0]
    assert action.status == "stopped"
    assert action.messages.sent == 1
    assert action.stopped_unsent == 2
    assert report.totals.stopped_unsent == 2
    assert report.events.paused == 1


def test_the_email_goes_once_to_each_person_on_the_list() -> None:
    settings = get_settings().model_copy(
        update={"agent_report_recipients": "first@example.com, second@example.com"}
    )
    with SessionLocal() as session:
        run_id = _run(session).run_id

    mailer = NullMailer()
    assert send_run_report(run_id, settings=settings, mailer=mailer) == 2
    assert send_run_report(run_id, settings=settings, mailer=mailer) == 0

    assert sorted(message.to for message in mailer.sent_messages) == [
        "first@example.com",
        "second@example.com",
    ]
    assert mailer.sent_messages[0].subject == f"ACE run {run_id}: nothing new found"

    empty = get_settings().model_copy(update={"agent_report_recipients": ""})
    assert send_run_report(run_id, settings=empty, mailer=NullMailer()) == 0


def test_a_failing_report_never_fails_the_run(monkeypatch) -> None:
    def explode(run_id: int) -> int:
        raise RuntimeError("mail server is down")

    monkeypatch.setattr(run_report_email, "send_run_report", explode)
    assert announce_run_finished(1) is None
