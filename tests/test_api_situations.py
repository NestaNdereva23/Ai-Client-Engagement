from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.signals import recompute_new_client_signals
from app.agents.situations import NEW_CLIENT_SINGLE_DEPOSIT, recompute_new_client_single_deposit
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

DELTA_URL = f"/api/v1/agent/situations/{NEW_CLIENT_SINGLE_DEPOSIT}/delta"

FUND_ID = 9712
NEW_ARRIVAL_CLIENT = 971201
SECOND_DEPOSIT_CLIENT = 971202
WINDOW_CLIENT = 971203
PERSIST_CLIENT = 971204

CLIENT_IDS = (NEW_ARRIVAL_CLIENT, SECOND_DEPOSIT_CLIENT, WINDOW_CLIENT, PERSIST_CLIENT)

AS_OF = date(2026, 9, 15)
WINDOW_DAYS = 30


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


@pytest.fixture(autouse=True)
def fixed_window(monkeypatch):
    from app.agents import signals

    monkeypatch.setattr(signals, "active_threshold", lambda *args, **kwargs: WINDOW_DAYS)


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=100_000.0,
        n_deposits=1,
        n_withdrawals=0,
        first_deposit_date=AS_OF - timedelta(days=3),
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _purge(session) -> None:
    session.execute(
        delete(ClientSituationSnapshot).where(ClientSituationSnapshot.client_id.in_(CLIENT_IDS))
    )
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
                _fund(NEW_ARRIVAL_CLIENT),
                _fund(SECOND_DEPOSIT_CLIENT),
                _fund(WINDOW_CLIENT, first_deposit_date=AS_OF - timedelta(days=WINDOW_DAYS - 1)),
                _fund(PERSIST_CLIENT),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session)


def _recompute(as_of: date) -> None:
    with SessionLocal() as session:
        run = recompute_new_client_signals(session, as_of)
        recompute_new_client_single_deposit(session, as_of, run.run_id)


def test_a_run_with_new_arrivals_counts_them(book: None) -> None:
    _recompute(AS_OF)

    response = client.get(DELTA_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["situation_code"] == NEW_CLIENT_SINGLE_DEPOSIT
    assert body["run_id"] is not None
    assert body["newly_active"] == 4
    assert body["newly_resolved"] == 0
    assert body["persisting"] == 0


def test_a_run_with_both_kinds_of_resolution(book: None) -> None:
    _recompute(AS_OF)

    with SessionLocal() as session:
        fund = session.get(ActiveClientFund, (SECOND_DEPOSIT_CLIENT, FUND_ID))
        fund.n_deposits = 2
        session.commit()

    _recompute(AS_OF + timedelta(days=2))

    response = client.get(DELTA_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["newly_resolved"] == 2
    assert body["resolved_by_second_deposit"] == 1
    assert body["resolved_by_window_elapsed"] == 1
    assert body["resolved_by_both"] == 0


def test_a_run_with_no_change_at_all(book: None) -> None:
    _recompute(AS_OF)
    _recompute(AS_OF + timedelta(days=1))

    response = client.get(DELTA_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["newly_active"] == 0
    assert body["newly_resolved"] == 0
    assert body["persisting"] == 4
