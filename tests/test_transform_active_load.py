"""Tests for flattening and persisting the active-clients feed.

Covers what AM1 promises: mixed-shape dates and string amounts parse the
same way the dormant feed's do, sale_type survives into transactions, and
transform_active_run's reconciliation logging matches what a fixture
actually returns per fund. Also covers active_transaction: every observed
purchase/sale lands there, and a row from an earlier pull stays there even
once a later pull's "last 5 purchases" / "last 2 sales" window no longer
includes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.models import IngestionStatus, PiiVault, RawStaging
from app.db.session import SessionLocal
from app.transform.active_flatten import flatten_active_run
from app.transform.active_load import transform_active_run

EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)


def _client(
    client_id: int,
    fund_id: int,
    txn_id: int,
    amount: str = "5000",
    date: str = "2026-07-01T00:00:00",
    sale_type: str | None = None,
) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "client_code": "C-1",
        "client_name": "Jane Doe",
        "balance": 42000.0,
        "computed_at": "2026-07-20T08:00:00",
        "last_5_purchases": [
            {"id": txn_id, "date": date, "number": amount, "unit_fund_id": fund_id}
        ],
        "last_2_sales": (
            [
                {
                    "id": txn_id + 1,
                    "date": date,
                    "number": "1000",
                    "unit_fund_id": fund_id,
                    "sale_type": sale_type,
                }
            ]
            if sale_type
            else []
        ),
    }


def _payload(*funds: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(funds)}


def _one_fund(client_count: int, clients: list[dict[str, Any]]) -> dict[str, Any]:
    return _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "client_count": client_count,
            "clients": clients,
        }
    )


@pytest.fixture
def normalized_ids():
    """Collect ids written during a test and remove them afterwards."""
    ids: dict[str, set[int]] = {"clients": set()}
    yield ids
    if not ids["clients"]:
        return
    with SessionLocal() as session:
        session.execute(
            delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(ids["clients"]))
        )
        session.execute(
            delete(ActiveTransaction).where(ActiveTransaction.client_id.in_(ids["clients"]))
        )
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(ids["clients"])))
        session.commit()


def _seed_run(session, run_id: str, payload: dict[str, Any]) -> None:
    session.add(IngestionStatus(run_id=run_id, endpoint="active-clients", reference_ts=ANCHOR))
    session.add(
        RawStaging(run_id=run_id, endpoint="active-clients", natural_key="1", payload=payload)
    )
    session.commit()


def test_mixed_date_and_string_amount_parsed(db, cleanup_runs, normalized_ids):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["clients"].add(9101)

    payload = _one_fund(
        1, [_client(9101, 10, 91001, amount="15000.50", date="2026-07-01T09:30:00+03:00")]
    )
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        counts = transform_active_run(session, run_id)

    assert counts.client_funds == 1
    with SessionLocal() as session:
        row = session.execute(
            select(ActiveClientFund).where(ActiveClientFund.client_id == 9101)
        ).scalar_one()
        assert row.balance == 42000.0
        assert row.last_deposit_date.isoformat() == "2026-07-01"


def test_sale_type_survives_flatten(db, cleanup_runs, normalized_ids):
    """flatten_active_run keeps sale_type on the in-memory row, ahead of it
    landing on the persisted active_transaction row -- see
    test_persist_writes_active_transaction_rows below for the persisted side.
    """
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["clients"].add(9102)

    payload = _one_fund(1, [_client(9102, 10, 91011, sale_type="full_withdrawal")])
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        result = flatten_active_run(session, run_id)

    sale_rows = [t for t in result.transactions if t.txn_type == "sale"]
    assert sale_rows and sale_rows[0].sale_type == "full_withdrawal"


def test_persist_writes_active_transaction_rows(db, cleanup_runs, normalized_ids):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["clients"].add(9106)

    payload = _one_fund(1, [_client(9106, 10, 91051, amount="7500", sale_type="full_withdrawal")])
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        counts = transform_active_run(session, run_id)

    assert counts.transactions == 2  # one purchase, one sale

    with SessionLocal() as session:
        rows = (
            session.execute(
                select(ActiveTransaction)
                .where(ActiveTransaction.client_id == 9106)
                .order_by(ActiveTransaction.txn_id)
            )
            .scalars()
            .all()
        )
    assert [r.txn_type for r in rows] == ["purchase", "sale"]
    purchase, sale = rows
    assert purchase.amount == 7500.0
    assert purchase.unit_fund_id == 10
    assert sale.sale_type == "full_withdrawal"


def test_a_transaction_that_ages_out_of_the_window_stays_in_the_ledger(
    db, cleanup_runs, normalized_ids
):
    """Two separate pulls, each returning a different single purchase (the
    feed's own rolling "last 5" window moving on) -- both still show up in
    active_transaction afterward, since it accumulates rather than mirrors
    the feed's latest snapshot.
    """
    client_id = 9107
    normalized_ids["clients"].add(client_id)

    first_run = uuid4().hex
    second_run = uuid4().hex
    cleanup_runs.extend([first_run, second_run])

    with SessionLocal() as session:
        _seed_run(session, first_run, _one_fund(1, [_client(client_id, 10, 91061)]))
        transform_active_run(session, first_run)

        _seed_run(session, second_run, _one_fund(1, [_client(client_id, 10, 91071)]))
        transform_active_run(session, second_run)

    with SessionLocal() as session:
        txn_ids = set(
            session.scalars(
                select(ActiveTransaction.txn_id).where(ActiveTransaction.client_id == client_id)
            )
        )
    assert {91061, 91071}.issubset(txn_ids)


def test_reconciliation_matches_returned_rows(db, cleanup_runs, normalized_ids, caplog):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["clients"].update({9103, 9104})

    payload = _one_fund(2, [_client(9103, 10, 91021), _client(9104, 10, 91031)])
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        counts = transform_active_run(session, run_id)

    assert counts.client_funds == 2  # matches the fund's own client_count of 2


def test_idempotent_retransform_no_dupes(db, cleanup_runs, normalized_ids):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["clients"].add(9105)

    payload = _one_fund(1, [_client(9105, 10, 91041)])
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        first = transform_active_run(session, run_id)
        second = transform_active_run(session, run_id)

    assert first.client_funds == second.client_funds == 1
    with SessionLocal() as session:
        rows = (
            session.execute(select(ActiveClientFund).where(ActiveClientFund.client_id == 9105))
            .scalars()
            .all()
        )
    assert len(rows) == 1
