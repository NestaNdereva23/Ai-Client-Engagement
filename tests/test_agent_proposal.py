"""The proposal tables themselves: a proposal can be created by hand, and a
client left out of it always carries a reason.

client_id and unit_fund_id on agent_proposal_client are plain numbers with no
foreign key, the same choice already made for active_client_fund, because a
proposal can be built from either the dormant book or the active book.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.session import SessionLocal

_FUND_ID = 994
_CLIENT_A = 99401
_CLIENT_B = 99402
_CLIENT_C = 99403


@pytest.fixture
def proposal(db: None):
    with SessionLocal() as session:
        row = AgentProposal(
            action_code="start_win_back",
            catalog_version=1,
            group_name="test proposal group",
            client_count=3,
            evidence="three dormant clients, no contact in ninety days",
            reason="win back before they close the account",
            permission_applied="suggest_only",
        )
        session.add(row)
        session.commit()
        proposal_id = row.proposal_id

    yield proposal_id

    with SessionLocal() as session:
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id == proposal_id)
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


def test_a_proposal_can_be_created_by_hand(proposal: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentProposal, proposal)
    assert row.status == "proposed"
    assert row.decided_by is None
    assert row.campaign_id is None


def test_an_included_client_needs_no_reason(proposal: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentProposalClient(
                proposal_id=proposal,
                client_id=_CLIENT_A,
                unit_fund_id=_FUND_ID,
                included=True,
            )
        )
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal,
                AgentProposalClient.client_id == _CLIENT_A,
            )
        )
    assert row.included is True
    assert row.skip_reason is None


def test_a_left_out_client_must_carry_a_reason(proposal: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentProposalClient(
                proposal_id=proposal,
                client_id=_CLIENT_B,
                unit_fund_id=_FUND_ID,
                included=False,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with SessionLocal() as session:
        row = session.scalar(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal,
                AgentProposalClient.client_id == _CLIENT_B,
            )
        )
    assert row is None


def test_a_left_out_client_with_a_reason_is_accepted(proposal: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentProposalClient(
                proposal_id=proposal,
                client_id=_CLIENT_B,
                unit_fund_id=_FUND_ID,
                included=False,
                skip_reason="opted out last week",
            )
        )
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(AgentProposalClient).where(
                AgentProposalClient.proposal_id == proposal,
                AgentProposalClient.client_id == _CLIENT_B,
            )
        )
    assert row.included is False
    assert row.skip_reason == "opted out last week"


def test_the_same_client_and_fund_cannot_appear_twice_on_one_proposal(proposal: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentProposalClient(
                proposal_id=proposal,
                client_id=_CLIENT_C,
                unit_fund_id=_FUND_ID,
                included=True,
            )
        )
        session.commit()

    with SessionLocal() as session:
        session.add(
            AgentProposalClient(
                proposal_id=proposal,
                client_id=_CLIENT_C,
                unit_fund_id=_FUND_ID,
                included=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
