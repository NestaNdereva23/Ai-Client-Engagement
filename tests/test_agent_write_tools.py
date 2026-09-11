"""The write tools: the agent records a decision and starts real work, and
every attempt to go around a check is refused and recorded.

Each tool is called the way the executor calls it, session first, so these
prove the tool itself rather than the conversation around it.
"""

from __future__ import annotations

import ast
import os
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents import insight_members as insight_members_module
from app.agents.proposal_state import transition_proposal
from app.agents.propose import ON_DO_NOT_CONTACT_LIST
from app.agents.watchlist import FEES_WILL_EMPTY, WatchlistThresholds
from app.agents.write_tools import (
    FLAG_FOR_ACCOUNT_MANAGER,
    RECORD_NO_ACTION,
    RUN_PROPOSAL,
    WRITE_PROPOSAL,
    make_write_tools,
)
from app.db.models.active_clients import (
    FLAGGED_FOR_ACCOUNT_MANAGER,
    ActiveClientFund,
    ActiveClientInteraction,
)
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep, Enrollment
from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.outreach import Campaign
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.digest.build import is_deprioritized, latest_interactions_for

FUND_ID = 9745
ELIGIBLE_CLIENT = 974501
SUPPRESSED_CLIENT = 974502

AS_OF = date(2026, 9, 9)

RUN_ID = None

INSIGHT_TITLE = "A group the write tools were tried against"

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=100.0,
    awaiting_call_days=2,
)


def _purge(session) -> None:
    client_ids = (ELIGIBLE_CLIENT, SUPPRESSED_CLIENT)
    campaign_ids = session.scalars(
        select(Campaign.campaign_id).where(Campaign.campaign_type == "agent_proposal")
    ).all()
    if campaign_ids:
        session.execute(delete(Enrollment).where(Enrollment.campaign_id.in_(campaign_ids)))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id.in_(campaign_ids)))

    insight_ids = session.scalars(
        select(AgentInsight.insight_id).where(AgentInsight.title == INSIGHT_TITLE)
    ).all()
    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id).where(AgentProposal.insight_id.in_(insight_ids or [0]))
    ).all()
    if proposal_ids:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id.in_([str(pid) for pid in proposal_ids]),
            )
        )
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids))
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(proposal_ids)))
    if campaign_ids:
        session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
    if insight_ids:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_insight",
                AuditLog.entity_id.in_([str(i) for i in insight_ids]),
            )
        )
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))

    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "active_client_interaction",
            AuditLog.entity_id.in_([f"{cid}/{FUND_ID}" for cid in client_ids]),
        )
    )
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id.in_(client_ids))
    )
    session.execute(delete(Suppression).where(Suppression.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.execute(
        delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id.in_(client_ids))
    )
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(client_ids)))
    session.execute(delete(Clients).where(Clients.client_id.in_(client_ids)))
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
def _fixed_thresholds(monkeypatch, clean: None):
    monkeypatch.setattr(
        insight_members_module, "load_thresholds", lambda session, as_of: THRESHOLDS
    )


class FakeDrafter:
    """Stands in for the drafting run, recording what it was asked to draft."""

    def __init__(self, drafted: int = 1) -> None:
        self.drafted = drafted
        self.calls: list[dict] = []

    def __call__(self, session, *, campaign_id: int, prohibitions) -> int:
        self.calls.append({"campaign_id": campaign_id, "prohibitions": tuple(prohibitions)})
        return self.drafted


def _tools(*, draft=None):
    return make_write_tools(run_id=RUN_ID, as_of=AS_OF, draft=draft or FakeDrafter())


def _seed_client(client_id: int) -> None:
    """One client of the active book, with the rows enrolling one needs."""
    with SessionLocal() as session:
        if session.get(Funds, FUND_ID) is None:
            session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="A money market fund"))
            session.flush()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                balance=200_000.0,
                n_purchases_returned=5,
                n_sales_returned=0,
                total_purchase_amount=200_000.0,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                balance=200_000.0,
                n_deposits=5,
                n_withdrawals=0,
                months_until_empty=2.0,
            )
        )
        session.commit()


def _write_insight(*, state: str = "accepted") -> int:
    with SessionLocal() as session:
        insight = AgentInsight(
            kind="risk",
            title=INSIGHT_TITLE,
            group_name=FEES_WILL_EMPTY,
            client_count=1,
            money_total_kes=200_000.0,
            confidence="high",
            confidence_reason="every client in it was counted the same way twice",
            suggestion="tell them before the balance runs out",
            avoid_saying="that their account is nearly empty",
            why_now="the fee takes another month off every one of them",
            state=state,
            dismissed_reason="not worth acting on" if state == "dismissed" else None,
        )
        session.add(insight)
        session.commit()
        return insight.insight_id


def _proposed(session, tools, insight_id: int) -> dict:
    result = tools[WRITE_PROPOSAL](
        session,
        insight_id=insight_id,
        action_code="fee_warning",
        angle="fee_warning",
        reason="their balance runs out within a few months at the current fee",
    )
    session.commit()
    return result


def test_write_proposal_records_the_response_and_its_reason() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight()

    with SessionLocal() as session:
        result = _proposed(session, _tools(), insight_id)

    assert result["status"] == "proposed"
    assert result["included_count"] == 1

    with SessionLocal() as session:
        proposal = session.get(AgentProposal, result["proposal_id"])
        insight = session.get(AgentInsight, insight_id)
        audit_actions = session.scalars(
            select(AuditLog.action).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id == str(result["proposal_id"]),
            )
        ).all()

    assert proposal.insight_id == insight_id
    assert proposal.action_code == "fee_warning"
    assert proposal.reason.startswith("their balance runs out")
    assert proposal.status == "proposed"
    assert insight.state == "acted_on"
    assert "create" in audit_actions


def test_write_proposal_refuses_a_finding_nobody_accepted() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight(state="dismissed")

    with SessionLocal() as session:
        result = _proposed(session, _tools(), insight_id)

    assert result["error"] == "insight_not_accepted"
    with SessionLocal() as session:
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.insight_id == insight_id)
        ).all()
    assert proposals == []


def test_write_proposal_refuses_an_action_the_catalogue_does_not_carry() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight()

    with SessionLocal() as session:
        result = _tools()[WRITE_PROPOSAL](
            session,
            insight_id=insight_id,
            action_code="send_them_a_hamper",
            reason="it would cheer them up",
        )
        session.rollback()

    assert result["error"] == "unknown_action"


def test_write_proposal_refuses_when_every_client_is_left_out() -> None:
    _seed_client(SUPPRESSED_CLIENT)
    with SessionLocal() as session:
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="asked us to stop"))
        session.commit()
    insight_id = _write_insight()

    with SessionLocal() as session:
        result = _proposed(session, _tools(), insight_id)

    assert result["error"] == "every_client_was_left_out"
    with SessionLocal() as session:
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.insight_id == insight_id)
        ).all()
    assert proposals == []


def test_run_proposal_creates_the_campaign_enrolls_and_drafts_into_review() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight()
    drafter = FakeDrafter(drafted=1)
    tools = _tools(draft=drafter)

    with SessionLocal() as session:
        proposal_id = _proposed(session, tools, insight_id)["proposal_id"]
        transition_proposal(
            session,
            session.get(AgentProposal, proposal_id),
            to_status="approved",
            reason="a person said yes",
            decided_by="reviewer_1",
        )
        session.commit()

        result = tools[RUN_PROPOSAL](session, proposal_id=proposal_id)

    assert result["status"] == "running"
    assert result["enrolled_count"] == 1
    assert result["drafted_count"] == 1
    campaign_id = result["campaign_id"]

    with SessionLocal() as session:
        proposal = session.get(AgentProposal, proposal_id)
        campaign = session.get(Campaign, campaign_id)
        enrolled = session.scalars(
            select(Enrollment.client_id).where(Enrollment.campaign_id == campaign_id)
        ).all()
        steps = session.scalars(
            select(CampaignStep).where(CampaignStep.campaign_id == campaign_id)
        ).all()
        audit_actions = session.scalars(
            select(AuditLog.action).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id == str(proposal_id),
            )
        ).all()

    assert proposal.status == "running"
    assert proposal.campaign_id == campaign_id
    assert campaign.status == "running"
    assert list(enrolled) == [ELIGIBLE_CLIENT]
    assert [step.message_angle for step in steps] == ["fee_warning"]
    assert "run" in audit_actions
    assert drafter.calls[0]["campaign_id"] == campaign_id
    assert any("nearly empty" in line for line in drafter.calls[0]["prohibitions"])


def test_run_proposal_refuses_one_nobody_approved() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight()
    drafter = FakeDrafter()
    tools = _tools(draft=drafter)

    with SessionLocal() as session:
        proposal_id = _proposed(session, tools, insight_id)["proposal_id"]
        result = tools[RUN_PROPOSAL](session, proposal_id=proposal_id)
        session.commit()

    assert result["error"] == "proposal_not_approved"
    assert drafter.calls == []
    with SessionLocal() as session:
        proposal = session.get(AgentProposal, proposal_id)
        campaigns = session.scalars(
            select(Campaign).where(Campaign.campaign_type == "agent_proposal")
        ).all()
    assert proposal.status == "proposed"
    assert proposal.campaign_id is None
    assert campaigns == []


def test_run_proposal_drops_a_client_suppressed_since_it_was_written() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    _seed_client(SUPPRESSED_CLIENT)
    insight_id = _write_insight()
    drafter = FakeDrafter()
    tools = _tools(draft=drafter)

    with SessionLocal() as session:
        proposal_id = _proposed(session, tools, insight_id)["proposal_id"]
        transition_proposal(
            session,
            session.get(AgentProposal, proposal_id),
            to_status="approved",
            reason="a person said yes",
            decided_by="reviewer_1",
        )
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="asked us to stop"))
        session.commit()

        result = tools[RUN_PROPOSAL](session, proposal_id=proposal_id)

    assert result["enrolled_count"] == 1
    assert result["dropped_since_proposed"] == {ON_DO_NOT_CONTACT_LIST: 1}

    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentProposalClient)
            .where(AgentProposalClient.proposal_id == proposal_id)
            .order_by(AgentProposalClient.client_id)
        ).all()
        enrolled = session.scalars(
            select(Enrollment.client_id).where(Enrollment.campaign_id == result["campaign_id"])
        ).all()

    assert [(row.client_id, row.included, row.skip_reason) for row in rows] == [
        (ELIGIBLE_CLIENT, True, None),
        (SUPPRESSED_CLIENT, False, ON_DO_NOT_CONTACT_LIST),
    ]
    assert list(enrolled) == [ELIGIBLE_CLIENT]


def test_record_no_action_keeps_the_group_the_count_and_the_reason() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight()

    with SessionLocal() as session:
        result = _tools()[RECORD_NO_ACTION](
            session,
            insight_id=insight_id,
            reason="the fee change lands next week and fixes this on its own",
        )
        session.commit()

    assert result["status"] == "recorded"
    assert result["group_name"] == FEES_WILL_EMPTY
    assert result["client_count"] == 1

    with SessionLocal() as session:
        proposal = session.get(AgentProposal, result["proposal_id"])
        rows = session.scalars(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == result["proposal_id"]
            )
        ).all()

    assert proposal.action_code == "do_nothing"
    assert proposal.insight_id == insight_id
    assert proposal.reason.startswith("the fee change")
    assert [(row.included, row.skip_reason) for row in rows] == [(False, "no_action_decided")]


def test_record_no_action_needs_a_reason() -> None:
    _seed_client(ELIGIBLE_CLIENT)
    insight_id = _write_insight()

    with SessionLocal() as session:
        result = _tools()[RECORD_NO_ACTION](session, insight_id=insight_id, reason="  ")
        session.rollback()

    assert result["error"] == "missing_reason"


def test_flagging_leaves_a_note_and_does_not_hide_the_client() -> None:
    _seed_client(ELIGIBLE_CLIENT)

    with SessionLocal() as session:
        result = _tools()[FLAG_FOR_ACCOUNT_MANAGER](
            session,
            client_id=ELIGIBLE_CLIENT,
            unit_fund_id=FUND_ID,
            note="their balance runs out before the next review",
        )

    assert result["status"] == "flagged"

    with SessionLocal() as session:
        row = session.get(ActiveClientInteraction, result["interaction_id"])
        audit_actions = session.scalars(
            select(AuditLog.action).where(
                AuditLog.entity_type == "active_client_interaction",
                AuditLog.entity_id == f"{ELIGIBLE_CLIENT}/{FUND_ID}",
            )
        ).all()
        handled = latest_interactions_for(session, [(ELIGIBLE_CLIENT, FUND_ID)])

    assert row.type == FLAGGED_FOR_ACCOUNT_MANAGER
    assert row.note.startswith("their balance runs out")
    assert FLAGGED_FOR_ACCOUNT_MANAGER in audit_actions
    assert handled == {}


def test_a_flag_never_pushes_a_client_down_the_morning_list() -> None:
    _seed_client(ELIGIBLE_CLIENT)

    class Line:
        client_id = ELIGIBLE_CLIENT
        unit_fund_id = FUND_ID
        risk_band = "High"

    with SessionLocal() as session:
        _tools()[FLAG_FOR_ACCOUNT_MANAGER](
            session,
            client_id=ELIGIBLE_CLIENT,
            unit_fund_id=FUND_ID,
            note="worth a call before the fee lands",
        )
        touched = latest_interactions_for(session, [(ELIGIBLE_CLIENT, FUND_ID)])

    assert is_deprioritized(Line(), touched) is False


def test_flagging_refuses_a_client_fund_that_does_not_exist() -> None:
    with SessionLocal() as session:
        result = _tools()[FLAG_FOR_ACCOUNT_MANAGER](
            session, client_id=ELIGIBLE_CLIENT, unit_fund_id=FUND_ID, note="look at this"
        )

    assert result["error"] == "unknown_client_fund"


def _module_imports(paths: dict[str, str], module: str) -> set[str]:
    tree = ast.parse(open(paths[module], encoding="utf8").read())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {name for name in found if name in paths}


def _reachable_from(module: str) -> set[str]:
    paths: dict[str, str] = {}
    for root, _, files in os.walk(os.path.join("src", "app")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            dotted = path[len("src") + 1 : -len(".py")].replace(os.sep, ".")
            paths[dotted.removesuffix(".__init__")] = path

    seen: set[str] = set()
    pending = [module]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(_module_imports(paths, current))
    return seen


@pytest.mark.parametrize(
    "module",
    ["app.agents.write_tools", "app.agents.tools", "app.agents.insight_tools"],
)
def test_no_agent_tool_can_reach_the_mailer(module: str) -> None:
    """Sending stays behind the send gate, so no tool the agent can call may
    even import its way to the mailer.
    """
    reachable = _reachable_from(module)
    assert "app.delivery.mailer" not in reachable
    assert "app.delivery.sender" not in reachable
    assert "app.services.campaigns" not in reachable
