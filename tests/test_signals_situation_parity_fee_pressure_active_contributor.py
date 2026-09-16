from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.agents.signals import recompute_fee_pressure_signal
from app.agents.situations import recompute_fee_pressure_active_contributor
from app.agents.watchlist import WatchGroup, WatchlistThresholds, fee_pressure_active_contributor
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, ClientSituationState
from app.db.session import SessionLocal

FUND_ID = 9560
ACTIVE_UNDER_PRESSURE_CLIENT = 956001
DORMANT_UNDER_PRESSURE_CLIENT = 956002
ACTIVE_NOT_UNDER_PRESSURE_CLIENT = 956003
NO_RISK_ROW_CLIENT = 956004

CLIENT_IDS = (
    ACTIVE_UNDER_PRESSURE_CLIENT,
    DORMANT_UNDER_PRESSURE_CLIENT,
    ACTIVE_NOT_UNDER_PRESSURE_CLIENT,
    NO_RISK_ROW_CLIENT,
)

AS_OF = date(2026, 9, 16)
MONTHS_UNTIL_EMPTY = 6.0

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=MONTHS_UNTIL_EMPTY,
    small_balance=100.0,
    awaiting_call_days=2,
)


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=4_000.0,
        n_deposits=5,
        n_withdrawals=0,
        months_until_empty=2.5,
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _risk(client_id: int, **overrides) -> ClientRiskFeatures:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        sig_heavy_withdrawal=False,
        sig_dormant=False,
        sig_broken_pattern=False,
        sig_shrinking=False,
        sig_going_dormant=False,
        sig_never_repeated=False,
        risk_score=10,
        risk_band="Watch",
        risk_reasons="no signal",
        fund_at_risk=0.0,
        config_version=1,
    )
    row.update(overrides)
    return ClientRiskFeatures(**row)


def _purge(session, client_ids: tuple[int, ...]) -> None:
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(client_ids))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(client_ids)))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add_all(
            [
                _fund(ACTIVE_UNDER_PRESSURE_CLIENT),
                _fund(DORMANT_UNDER_PRESSURE_CLIENT),
                _fund(ACTIVE_NOT_UNDER_PRESSURE_CLIENT, months_until_empty=None),
                _fund(NO_RISK_ROW_CLIENT),
            ]
        )
        session.add_all(
            [
                _risk(ACTIVE_UNDER_PRESSURE_CLIENT, sig_dormant=False),
                _risk(DORMANT_UNDER_PRESSURE_CLIENT, sig_dormant=True),
                _risk(ACTIVE_NOT_UNDER_PRESSURE_CLIENT, sig_dormant=False),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)


def _keys(group: WatchGroup) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def _recompute(session) -> None:
    run = recompute_fee_pressure_signal(session, AS_OF, MONTHS_UNTIL_EMPTY)
    recompute_fee_pressure_active_contributor(session, AS_OF, run.run_id)


def _group() -> WatchGroup:
    with SessionLocal() as session:
        return fee_pressure_active_contributor(session, THRESHOLDS, AS_OF)


def test_a_client_still_paying_in_is_included(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group()

    assert (ACTIVE_UNDER_PRESSURE_CLIENT, FUND_ID) in _keys(group)


def test_a_dormant_client_under_fee_pressure_is_excluded(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group()

    assert (DORMANT_UNDER_PRESSURE_CLIENT, FUND_ID) not in _keys(group)


def test_a_client_not_under_fee_pressure_is_excluded(book: None) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group()

    assert (ACTIVE_NOT_UNDER_PRESSURE_CLIENT, FUND_ID) not in _keys(group)


def test_a_client_with_no_risk_row_is_included(book: None) -> None:
    """No risk row means no dormant flag to exclude on, so fee pressure alone is enough."""
    with SessionLocal() as session:
        _recompute(session)

    group = _group()

    assert (NO_RISK_ROW_CLIENT, FUND_ID) in _keys(group)
