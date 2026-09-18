"""Proves the situation table is a faithful stand in for signed_up_recently's
own SQL: the same clients, included or excluded for the same reasons, once
run through the real propose gates.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.agents.propose import GROUP_ACTIONS, group_skip_reasons, load_action_or_raise
from app.agents.signals import recompute_new_client_signals
from app.agents.situations import recompute_new_client_single_deposit
from app.agents.watchlist import (
    SIGNED_UP_RECENTLY,
    GroupMember,
    WatchGroup,
    WatchlistThresholds,
    signed_up_recently,
)
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.complaints import ClientComplaint
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, ClientSituationState
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal

FUND_ID = 9521
CLEAN_CLIENT = 952101
SUPPRESSED_CLIENT = 952102
COMPLAINT_CLIENT = 952103
CONTACTED_CLIENT = 952104
OUT_OF_WINDOW_CLIENT = 952105
SECOND_DEPOSIT_CLIENT = 952106

CLIENT_IDS = (
    CLEAN_CLIENT,
    SUPPRESSED_CLIENT,
    COMPLAINT_CLIENT,
    CONTACTED_CLIENT,
    OUT_OF_WINDOW_CLIENT,
    SECOND_DEPOSIT_CLIENT,
)

AS_OF = date(2026, 9, 15)
REVIEWER = "situation-parity-test"

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)


@pytest.fixture(autouse=True)
def fixed_window(monkeypatch):
    from app.agents import signals

    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: 30)


@pytest.fixture(autouse=True)
def situations_source(monkeypatch):
    from app.agents import watchlist

    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source="situations")
    )


def legacy_signed_up_recently(session, thresholds, as_of: date) -> WatchGroup:
    """The query signed_up_recently() ran before it read the situation table.

    Kept here only to prove the situation table agrees with it; it is not
    called anywhere else.
    """
    earliest = as_of - timedelta(days=thresholds.new_client_days)
    statement = select(
        ActiveClientFund.client_id, ActiveClientFund.unit_fund_id, ActiveClientFund.balance
    ).where(
        ActiveClientFund.n_deposits == 1,
        ActiveClientFund.first_deposit_date.is_not(None),
        ActiveClientFund.first_deposit_date >= earliest,
    )
    members = tuple(
        GroupMember(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=float(row.balance or 0.0),
        )
        for row in session.execute(statement).all()
    )
    return WatchGroup(name=SIGNED_UP_RECENTLY, members=members)


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=250_000.0,
        n_deposits=1,
        n_withdrawals=0,
        first_deposit_date=AS_OF - timedelta(days=3),
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _purge(session) -> None:
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(ClientComplaint).where(ClientComplaint.client_id.in_(CLIENT_IDS)))
    session.execute(delete(Suppression).where(Suppression.client_id.in_(CLIENT_IDS)))
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(CLIENT_IDS))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(CLIENT_IDS))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(CLIENT_IDS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENT_IDS)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)
        session.add_all(
            [
                _fund(CLEAN_CLIENT),
                _fund(SUPPRESSED_CLIENT),
                _fund(COMPLAINT_CLIENT),
                _fund(CONTACTED_CLIENT),
                _fund(OUT_OF_WINDOW_CLIENT, first_deposit_date=AS_OF - timedelta(days=90)),
                _fund(SECOND_DEPOSIT_CLIENT, n_deposits=2),
            ]
        )
        session.add(Suppression(client_id=SUPPRESSED_CLIENT, reason="opted out"))
        session.add(
            ClientComplaint(
                client_id=COMPLAINT_CLIENT,
                opened_at=AS_OF - timedelta(days=1),
                status="open",
                category="service",
                channel="call",
            )
        )
        session.add(
            ActiveClientInteraction(
                client_id=CONTACTED_CLIENT,
                unit_fund_id=FUND_ID,
                type="email_sent",
                reviewer_id=REVIEWER,
                created_at=AS_OF,
            )
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def _keys(group: WatchGroup) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def test_group_action_still_maps_to_welcome_and_top_up() -> None:
    assert GROUP_ACTIONS.get(SIGNED_UP_RECENTLY) == "welcome_and_top_up"


def test_situation_group_matches_the_legacy_query_client_for_client(book: None) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    with SessionLocal() as session:
        legacy = legacy_signed_up_recently(session, THRESHOLDS, AS_OF)
        situation = signed_up_recently(session, THRESHOLDS, AS_OF)

    assert _keys(legacy) == _keys(situation)
    assert _keys(situation) == {
        (CLEAN_CLIENT, FUND_ID),
        (SUPPRESSED_CLIENT, FUND_ID),
        (COMPLAINT_CLIENT, FUND_ID),
        (CONTACTED_CLIENT, FUND_ID),
    }


def test_the_propose_gates_give_the_same_reason_for_every_shared_member(book: None) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, AS_OF)
        recompute_new_client_single_deposit(session, AS_OF, run.run_id)

    with SessionLocal() as session:
        legacy = legacy_signed_up_recently(session, THRESHOLDS, AS_OF)
        situation = signed_up_recently(session, THRESHOLDS, AS_OF)

        action = load_action_or_raise(session, GROUP_ACTIONS[SIGNED_UP_RECENTLY], AS_OF)
        legacy_reasons = group_skip_reasons(session, legacy.members, action, AS_OF, None)
        situation_reasons = group_skip_reasons(session, situation.members, action, AS_OF, None)

    assert legacy_reasons == situation_reasons
    assert legacy_reasons[(CLEAN_CLIENT, FUND_ID)] is None
    assert legacy_reasons[(SUPPRESSED_CLIENT, FUND_ID)] == "on_do_not_contact_list"
    assert legacy_reasons[(COMPLAINT_CLIENT, FUND_ID)] == "open_complaint"
    assert legacy_reasons[(CONTACTED_CLIENT, FUND_ID)] == "contacted_recently"
