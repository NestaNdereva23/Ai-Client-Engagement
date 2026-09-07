"""Turning tonight's watch list into proposals with plain rules, no model.

Groups are built by hand for the eligibility tests, since the watch list
filters themselves already have their own tests. One end to end test builds
a small book and runs the real watch list, to prove a nightly run actually
leaves proposals behind.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.agents import propose as propose_module
from app.agents.propose import (
    ACTION_PAUSED,
    ANGLE_PAUSED,
    CONTACTED_RECENTLY,
    DO_NOTHING_ACTION,
    ON_DO_NOT_CONTACT_LIST,
    OPEN_COMPLAINT,
    ProposalActionMissing,
    propose_group,
    propose_watchlist,
)
from app.agents.watchlist import (
    FEES_WILL_EMPTY,
    GroupMember,
    WatchGroup,
    WatchlistThresholds,
)
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.complaints import ClientComplaint
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal

FUND_ID = 9520
ELIGIBLE_CLIENT = 952001
SUPPRESSED_CLIENT = 952002
COMPLAINT_CLIENT = 952003
CONTACTED_CLIENT = 952004

CLIENT_IDS = (ELIGIBLE_CLIENT, SUPPRESSED_CLIENT, COMPLAINT_CLIENT, CONTACTED_CLIENT)

AS_OF = date(2026, 9, 7)
REVIEWER = "propose-test-reviewer"
BOGUS_GROUP = "not_a_real_group"

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=100.0,
    awaiting_call_days=2,
)


def _member(client_id: int, balance: float = 400_000.0) -> GroupMember:
    return GroupMember(client_id=client_id, unit_fund_id=FUND_ID, balance=balance)


def _purge(session) -> None:
    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id)
        .join(AgentProposalClient, AgentProposalClient.proposal_id == AgentProposal.proposal_id)
        .where(AgentProposalClient.client_id.in_(CLIENT_IDS))
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
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(ClientComplaint).where(ClientComplaint.client_id.in_(CLIENT_IDS)))
    session.execute(delete(Suppression).where(Suppression.client_id.in_(CLIENT_IDS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENT_IDS)))
    session.commit()


@pytest.fixture
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


def _included(proposal_id: int) -> dict[int, str | None]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentProposalClient).where(AgentProposalClient.proposal_id == proposal_id)
        ).all()
    return {row.client_id: (True if row.included else row.skip_reason) for row in rows}


def test_an_empty_group_gets_no_proposal(clean: None) -> None:
    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=())
    with SessionLocal() as session:
        result = propose_group(session, group, THRESHOLDS, AS_OF)
    assert result is None


def test_an_eligible_client_is_included_under_the_mapped_action(clean: None) -> None:
    group = WatchGroup(
        name=FEES_WILL_EMPTY,
        definition={"months_until_empty_below": 6.0},
        members=(_member(ELIGIBLE_CLIENT),),
    )
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == "fee_warning"
    assert proposal.status == "proposed"
    assert proposal.permission_applied == "suggest_only"
    assert proposal.campaign_id is None
    assert _included(proposal_id) == {ELIGIBLE_CLIENT: True}


def test_a_suppressed_client_is_left_out_with_a_reason(clean: None) -> None:
    with SessionLocal() as session:
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="asked not to be contacted"))
        session.commit()

    group = WatchGroup(
        name=FEES_WILL_EMPTY,
        definition={},
        members=(_member(ELIGIBLE_CLIENT), _member(SUPPRESSED_CLIENT)),
    )
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == "fee_warning"
    included = _included(proposal_id)
    assert included[ELIGIBLE_CLIENT] is True
    assert included[SUPPRESSED_CLIENT] == ON_DO_NOT_CONTACT_LIST


def test_a_client_with_an_open_complaint_is_left_out_with_a_reason(clean: None) -> None:
    with SessionLocal() as session:
        session.add(
            ClientComplaint(
                client_id=COMPLAINT_CLIENT,
                opened_at=AS_OF - timedelta(days=1),
                status="open",
                category="service",
                channel="call",
            )
        )
        session.commit()

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(COMPLAINT_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == DO_NOTHING_ACTION
    assert _included(proposal_id)[COMPLAINT_CLIENT] == OPEN_COMPLAINT


def test_a_closed_complaint_does_not_exclude_a_client(clean: None) -> None:
    with SessionLocal() as session:
        session.add(
            ClientComplaint(
                client_id=COMPLAINT_CLIENT,
                opened_at=AS_OF - timedelta(days=30),
                closed_at=AS_OF - timedelta(days=20),
                status="closed",
                category="service",
                channel="call",
            )
        )
        session.commit()

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(COMPLAINT_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == "fee_warning"
    assert _included(proposal_id)[COMPLAINT_CLIENT] is True


def test_a_recently_contacted_client_is_left_out_with_a_reason(clean: None) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveClientInteraction(
                client_id=CONTACTED_CLIENT,
                unit_fund_id=FUND_ID,
                type="email_sent",
                reviewer_id=REVIEWER,
                created_at=datetime.combine(AS_OF - timedelta(days=1), datetime.min.time()),
            )
        )
        session.commit()

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(CONTACTED_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF, cooldown_days=7)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == DO_NOTHING_ACTION
    assert _included(proposal_id)[CONTACTED_CLIENT] == CONTACTED_RECENTLY


def test_a_contact_outside_the_cooldown_window_does_not_exclude_a_client(clean: None) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveClientInteraction(
                client_id=CONTACTED_CLIENT,
                unit_fund_id=FUND_ID,
                type="email_sent",
                reviewer_id=REVIEWER,
                created_at=datetime.combine(AS_OF - timedelta(days=30), datetime.min.time()),
            )
        )
        session.commit()

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(CONTACTED_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF, cooldown_days=7)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == "fee_warning"
    assert _included(proposal_id)[CONTACTED_CLIENT] is True


def test_a_snoozed_interaction_does_not_count_as_contact(clean: None) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveClientInteraction(
                client_id=CONTACTED_CLIENT,
                unit_fund_id=FUND_ID,
                type="snoozed",
                reviewer_id=REVIEWER,
                created_at=datetime.combine(AS_OF - timedelta(days=1), datetime.min.time()),
            )
        )
        session.commit()

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(CONTACTED_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == "fee_warning"
    assert _included(proposal_id)[CONTACTED_CLIENT] is True


def test_a_held_angle_leaves_the_client_out(clean: None, monkeypatch) -> None:
    monkeypatch.setattr(propose_module, "angle_is_held", lambda session, angle, at: True)

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(ELIGIBLE_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == DO_NOTHING_ACTION
    assert _included(proposal_id)[ELIGIBLE_CLIENT] == ANGLE_PAUSED


def test_a_paused_action_becomes_do_nothing_for_the_whole_group(clean: None, monkeypatch) -> None:
    monkeypatch.setattr(propose_module, "action_is_paused", lambda session, code, at: True)

    group = WatchGroup(
        name=FEES_WILL_EMPTY,
        definition={},
        members=(_member(ELIGIBLE_CLIENT), _member(SUPPRESSED_CLIENT)),
    )
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    assert proposal.action_code == DO_NOTHING_ACTION
    included = _included(proposal_id)
    assert included[ELIGIBLE_CLIENT] == ACTION_PAUSED
    assert included[SUPPRESSED_CLIENT] == ACTION_PAUSED


def test_a_client_with_two_funds_counts_once_in_the_qualifying_number(clean: None) -> None:
    second_fund = FUND_ID + 1
    group = WatchGroup(
        name=FEES_WILL_EMPTY,
        definition={},
        members=(
            _member(ELIGIBLE_CLIENT),
            GroupMember(client_id=ELIGIBLE_CLIENT, unit_fund_id=second_fund, balance=100_000.0),
        ),
    )
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()

    assert proposal.client_count == 1
    assert "1 clients qualify" in proposal.reason


def test_money_total_only_counts_the_included_clients(clean: None) -> None:
    with SessionLocal() as session:
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="opted out"))
        session.commit()

    group = WatchGroup(
        name=FEES_WILL_EMPTY,
        definition={},
        members=(
            _member(ELIGIBLE_CLIENT, balance=300_000.0),
            _member(SUPPRESSED_CLIENT, balance=700_000.0),
        ),
    )
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()

    assert proposal.client_count == 2
    assert proposal.money_total_kes == 300_000.0


def test_the_reason_and_evidence_are_written_in_plain_words(clean: None) -> None:
    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(ELIGIBLE_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()

    assert "Tell them the fee will empty the account" in proposal.reason
    assert "1 " in proposal.evidence or "1 clients" in proposal.evidence


def test_every_proposal_writes_an_audit_row(clean: None) -> None:
    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(ELIGIBLE_CLIENT),))
    with SessionLocal() as session:
        proposal = propose_group(session, group, THRESHOLDS, AS_OF)
        session.commit()
        proposal_id = proposal.proposal_id

    with SessionLocal() as session:
        row = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id == str(proposal_id),
                AuditLog.action == "create",
            )
        )
    assert row is not None
    assert row.detail["group_name"] == FEES_WILL_EMPTY


def test_a_group_missing_from_the_rule_table_is_refused(clean: None) -> None:
    group = WatchGroup(name=BOGUS_GROUP, definition={}, members=(_member(ELIGIBLE_CLIENT),))
    with SessionLocal() as session:
        with pytest.raises(ProposalActionMissing):
            propose_group(session, group, THRESHOLDS, AS_OF)


def test_a_rule_table_action_missing_from_the_catalogue_is_refused(
    clean: None, monkeypatch
) -> None:
    monkeypatch.setattr(propose_module, "load_action", lambda session, code, at: None)

    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=(_member(ELIGIBLE_CLIENT),))
    with SessionLocal() as session:
        with pytest.raises(ProposalActionMissing):
            propose_group(session, group, THRESHOLDS, AS_OF)


def test_propose_watchlist_proposes_and_excludes_across_a_full_run(
    clean: None, monkeypatch
) -> None:
    monkeypatch.setattr(propose_module, "load_thresholds", lambda session, as_of: THRESHOLDS)
    from app.agents import watchlist as watchlist_module

    monkeypatch.setattr(watchlist_module, "latest_completed_run_id", lambda session: None)

    with SessionLocal() as session:
        session.add(
            ActiveClientFund(
                client_id=ELIGIBLE_CLIENT,
                unit_fund_id=FUND_ID,
                balance=200_000.0,
                n_deposits=5,
                n_withdrawals=0,
                months_until_empty=2.0,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=SUPPRESSED_CLIENT,
                unit_fund_id=FUND_ID,
                balance=150_000.0,
                n_deposits=5,
                n_withdrawals=0,
                months_until_empty=2.0,
            )
        )
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="opted out"))
        session.commit()

    with SessionLocal() as session:
        proposals = propose_watchlist(session, AS_OF, thresholds=THRESHOLDS)
        session.commit()

    by_group = {p.group_name: p for p in proposals}
    assert FEES_WILL_EMPTY in by_group
    fee_proposal = by_group[FEES_WILL_EMPTY]
    assert fee_proposal.action_code == "fee_warning"

    included = _included(fee_proposal.proposal_id)
    assert included[ELIGIBLE_CLIENT] is True
    assert included[SUPPRESSED_CLIENT] == ON_DO_NOT_CONTACT_LIST
