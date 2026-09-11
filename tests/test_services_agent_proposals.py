"""Listing, reading, and deciding on agent proposals through the service layer."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.agents.proposal_state import InvalidTransition
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.pagination import MAX_LIMIT
from app.services.agent_proposals import (
    DailyUsage,
    ProposalNotFound,
    count_proposals,
    daily_usage,
    decide_proposal,
    get_proposal,
    get_proposal_clients,
    list_proposals,
)

FUND_ID = 9600
INCLUDED_CLIENT = 960001
SKIPPED_CLIENT = 960002

REVIEWER = "proposals-test-reviewer"


def _make_proposal(**overrides) -> AgentProposal:
    defaults = dict(
        action_code="start_win_back",
        catalog_version=1,
        group_name="very small and quiet",
        client_count=1,
        money_total_kes=1_000.0,
        evidence="one dormant client with a small, quiet balance",
        reason="win back before they close the account",
        permission_applied="suggest_only",
    )
    defaults.update(overrides)
    return AgentProposal(**defaults)


@pytest.fixture
def proposals(db: None):
    """Two proposals made moments apart, both today, in real insertion order."""
    with SessionLocal() as session:
        earlier = _make_proposal(status="proposed")
        session.add(earlier)
        session.flush()
        later = _make_proposal(group_name="fees will empty", action_code="fee_warning")
        session.add(later)
        session.commit()
        ids = [earlier.proposal_id, later.proposal_id]

    yield ids

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id.in_([str(i) for i in ids])))
        session.execute(delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(ids)))
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(ids)))
        session.commit()


@pytest.fixture
def proposal_from_yesterday(db: None):
    with SessionLocal() as session:
        row = _make_proposal(
            group_name="fees will empty",
            action_code="fee_warning",
            created_at=datetime.now() - timedelta(days=1),
        )
        session.add(row)
        session.commit()
        proposal_id = row.proposal_id

    yield proposal_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == str(proposal_id)))
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


@pytest.fixture
def proposal_with_clients(db: None):
    with SessionLocal() as session:
        row = _make_proposal(client_count=2)
        session.add(row)
        session.flush()
        session.add_all(
            [
                AgentProposalClient(
                    proposal_id=row.proposal_id,
                    client_id=INCLUDED_CLIENT,
                    unit_fund_id=FUND_ID,
                    included=True,
                ),
                AgentProposalClient(
                    proposal_id=row.proposal_id,
                    client_id=SKIPPED_CLIENT,
                    unit_fund_id=FUND_ID,
                    included=False,
                    skip_reason="on_do_not_contact_list",
                ),
            ]
        )
        session.commit()
        proposal_id = row.proposal_id

    yield proposal_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == str(proposal_id)))
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id == proposal_id)
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


def test_list_proposals_returns_newest_first(proposals: list[int]) -> None:
    earlier_id, later_id = proposals
    with SessionLocal() as session:
        rows, _ = list_proposals(session)
    ids = [proposal.proposal_id for proposal, _ in rows]
    assert ids.index(later_id) < ids.index(earlier_id)


def test_list_proposals_filters_by_status(proposals: list[int]) -> None:
    earlier_id, later_id = proposals
    with SessionLocal() as session:
        session.get(AgentProposal, later_id).status = "rejected"
        session.commit()

    with SessionLocal() as session:
        rows, _ = list_proposals(session, status="rejected")
    assert [proposal.proposal_id for proposal, _ in rows] == [later_id]


def test_list_proposals_filters_by_date(proposals: list[int], proposal_from_yesterday: int) -> None:
    with SessionLocal() as session:
        rows, _ = list_proposals(session, proposal_date=date.today())
    ids = {proposal.proposal_id for proposal, _ in rows}
    assert set(proposals) <= ids
    assert proposal_from_yesterday not in ids


def test_list_proposals_filters_by_action_code(proposals: list[int]) -> None:
    earlier_id, later_id = proposals
    with SessionLocal() as session:
        rows, _ = list_proposals(session, action_code="fee_warning")
    ids = {proposal.proposal_id for proposal, _ in rows}
    assert later_id in ids
    assert earlier_id not in ids


def test_list_proposals_excludes_an_action_code(proposals: list[int]) -> None:
    earlier_id, later_id = proposals
    with SessionLocal() as session:
        rows, _ = list_proposals(session, exclude_action_code="fee_warning")
    ids = {proposal.proposal_id for proposal, _ in rows}
    assert earlier_id in ids
    assert later_id not in ids


def test_list_proposals_filters_by_group_name(proposals: list[int]) -> None:
    earlier_id, later_id = proposals
    with SessionLocal() as session:
        rows, _ = list_proposals(session, group_name="fees will empty")
    ids = {proposal.proposal_id for proposal, _ in rows}
    assert later_id in ids
    assert earlier_id not in ids


def test_list_proposals_paginates_with_a_cursor(proposals: list[int]) -> None:
    earlier_id, later_id = proposals
    with SessionLocal() as session:
        first_page, cursor = list_proposals(session, limit=1)
        second_page, next_cursor = list_proposals(session, limit=1, cursor=cursor)
    assert [proposal.proposal_id for proposal, _ in first_page] == [later_id]
    assert cursor is not None
    assert [proposal.proposal_id for proposal, _ in second_page] == [earlier_id]
    assert next_cursor is None


def test_list_proposals_reports_how_many_clients_are_included(
    proposal_with_clients: int,
) -> None:
    with SessionLocal() as session:
        rows, _ = list_proposals(session, status="proposed")
    included_counts = {proposal.proposal_id: count for proposal, count in rows}
    assert included_counts[proposal_with_clients] == 1


def test_count_proposals_matches_the_same_filters(
    proposals: list[int], proposal_from_yesterday: int
) -> None:
    with SessionLocal() as session:
        total = count_proposals(session, proposal_date=date.today())
        rows, _ = list_proposals(session, proposal_date=date.today(), limit=MAX_LIMIT)
    assert len(rows) == min(total, MAX_LIMIT)
    assert proposal_from_yesterday not in {proposal.proposal_id for proposal, _ in rows}


def test_get_proposal_raises_when_missing(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(ProposalNotFound):
            get_proposal(session, 0)


def test_get_proposal_clients_returns_the_full_breakdown(proposal_with_clients: int) -> None:
    with SessionLocal() as session:
        rows = get_proposal_clients(session, proposal_with_clients)
    by_client = {row.client_id: row for row in rows}
    assert by_client[INCLUDED_CLIENT].included is True
    assert by_client[INCLUDED_CLIENT].skip_reason is None
    assert by_client[SKIPPED_CLIENT].included is False
    assert by_client[SKIPPED_CLIENT].skip_reason == "on_do_not_contact_list"


def test_decide_proposal_approves_and_records_who(proposal_with_clients: int) -> None:
    with SessionLocal() as session:
        proposal = decide_proposal(
            session,
            proposal_with_clients,
            decision="approve",
            reason="looks right, send it",
            decided_by=REVIEWER,
        )
        session.commit()
    assert proposal.status == "approved"
    assert proposal.decided_by == REVIEWER
    assert proposal.decided_at is not None

    with SessionLocal() as session:
        audit_row = session.scalar(
            select(AuditLog)
            .where(AuditLog.entity_id == str(proposal_with_clients))
            .order_by(AuditLog.created_at.desc())
        )
    assert audit_row is not None
    assert audit_row.detail["to"] == "approved"
    assert audit_row.detail["reason"] == "looks right, send it"


def test_decide_proposal_rejects(proposal_with_clients: int) -> None:
    with SessionLocal() as session:
        proposal = decide_proposal(
            session,
            proposal_with_clients,
            decision="reject",
            reason="not a good fit tonight",
            decided_by=REVIEWER,
        )
        session.commit()
    assert proposal.status == "rejected"
    assert proposal.decided_by == REVIEWER


def test_decide_proposal_twice_is_refused(proposal_with_clients: int) -> None:
    with SessionLocal() as session:
        decide_proposal(
            session,
            proposal_with_clients,
            decision="approve",
            reason="first pass",
            decided_by=REVIEWER,
        )
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(InvalidTransition):
            decide_proposal(
                session,
                proposal_with_clients,
                decision="reject",
                reason="second pass",
                decided_by=REVIEWER,
            )
        session.rollback()


def test_decide_proposal_raises_when_missing(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(ProposalNotFound):
            decide_proposal(
                session, 0, decision="approve", reason="does not exist", decided_by=REVIEWER
            )


@pytest.fixture
def usage_proposals(db: None):
    """Three proposals made today: one committed with a left out client, one
    rejected, and one for a different action, so daily_usage has a known
    answer to be checked against.
    """
    with SessionLocal() as session:
        committed = _make_proposal(
            action_code="fee_warning",
            group_name="fees will empty",
            client_count=3,
            money_total_kes=100_000.0,
            status="approved",
        )
        session.add(committed)
        session.flush()
        session.add_all(
            [
                AgentProposalClient(
                    proposal_id=committed.proposal_id,
                    client_id=960003,
                    unit_fund_id=FUND_ID,
                    included=True,
                ),
                AgentProposalClient(
                    proposal_id=committed.proposal_id,
                    client_id=960004,
                    unit_fund_id=FUND_ID,
                    included=True,
                ),
                AgentProposalClient(
                    proposal_id=committed.proposal_id,
                    client_id=960005,
                    unit_fund_id=FUND_ID,
                    included=False,
                    skip_reason="on_do_not_contact_list",
                ),
            ]
        )

        rejected = _make_proposal(
            action_code="fee_warning",
            group_name="fees will empty",
            client_count=1,
            money_total_kes=999_999.0,
            status="rejected",
        )
        session.add(rejected)
        session.flush()
        session.add(
            AgentProposalClient(
                proposal_id=rejected.proposal_id,
                client_id=960006,
                unit_fund_id=FUND_ID,
                included=True,
            )
        )

        other_action = _make_proposal(
            action_code="start_win_back",
            group_name="very small and quiet",
            client_count=1,
            money_total_kes=50_000.0,
            status="approved",
        )
        session.add(other_action)
        session.flush()
        session.add(
            AgentProposalClient(
                proposal_id=other_action.proposal_id,
                client_id=960007,
                unit_fund_id=FUND_ID,
                included=True,
            )
        )
        session.commit()
        ids = [committed.proposal_id, rejected.proposal_id, other_action.proposal_id]

    yield ids

    with SessionLocal() as session:
        session.execute(delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(ids)))
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(ids)))
        session.commit()


def test_daily_usage_counts_included_clients_and_committed_money(
    usage_proposals: list[int],
) -> None:
    with SessionLocal() as session:
        usage = daily_usage(session, action_code="fee_warning", as_of=date.today())
    assert usage == DailyUsage(used_clients=2, used_money_kes=100_000.0)


def test_daily_usage_ignores_a_rejected_proposal(usage_proposals: list[int]) -> None:
    with SessionLocal() as session:
        usage = daily_usage(session, action_code="fee_warning", as_of=date.today())
    assert usage.used_money_kes == 100_000.0


def test_daily_usage_ignores_a_different_action(usage_proposals: list[int]) -> None:
    with SessionLocal() as session:
        usage = daily_usage(session, action_code="start_win_back", as_of=date.today())
    assert usage == DailyUsage(used_clients=1, used_money_kes=50_000.0)


def test_daily_usage_is_zero_for_an_action_with_nothing_today(db: None) -> None:
    with SessionLocal() as session:
        usage = daily_usage(session, action_code="a_totally_unused_action", as_of=date.today())
    assert usage == DailyUsage(used_clients=0, used_money_kes=0.0)
