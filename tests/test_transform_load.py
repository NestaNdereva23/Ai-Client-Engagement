from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.db.models.models import (
    Clients,
    Funds,
    IngestionStatus,
    PiiVault,
    RawStaging,
    Transactions,
)
from app.db.session import SessionLocal
from app.transform.load import transform_run

EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)


def _client(client_id: int, fund_id: int, txn_id: int) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "client_code": "C-1",
        "client_name": "Jane Doe",
        "balance": 0,
        "computed_at": "2026-07-20T08:00:00",
        "last_5_purchases": [
            {"id": txn_id, "date": "2026-07-01T00:00:00", "number": "5000", "unit_fund_id": fund_id}
        ],
        "last_2_sales": [],
    }


def _payload(*funds: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(funds)}


def _one_fund_one_client() -> dict[str, Any]:
    return _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "inactive_client_count": 1,
            "clients": [_client(1001, 10, 5001)],
        }
    )


@pytest.fixture
def normalized_ids():
    """Collect ids written during a test and remove them afterwards.

    Empty means the test was skipped before writing, so the teardown touches no
    database.
    """
    ids: dict[str, set[int]] = {"funds": set(), "clients": set(), "txns": set()}
    yield ids
    if not (ids["funds"] or ids["clients"] or ids["txns"]):
        return
    with SessionLocal() as session:
        session.execute(delete(Transactions).where(Transactions.txn_id.in_(ids["txns"])))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(ids["clients"])))
        session.execute(delete(Clients).where(Clients.client_id.in_(ids["clients"])))
        session.execute(delete(Funds).where(Funds.unit_fund_id.in_(ids["funds"])))
        session.commit()


def _seed_run(session, run_id: str, payload: dict[str, Any]) -> None:
    session.add(IngestionStatus(run_id=run_id, endpoint="inactive-clients", reference_ts=ANCHOR))
    session.add(
        RawStaging(run_id=run_id, endpoint="inactive-clients", natural_key="1", payload=payload)
    )
    session.commit()


def test_clients_table_has_no_client_name() -> None:
    assert "client_name" not in Clients.__table__.columns


def test_transform_run_persists_normalized_rows(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].add(5001)

    with SessionLocal() as session:
        _seed_run(session, run_id, _one_fund_one_client())
        counts = transform_run(session, run_id)

    assert (counts.funds, counts.clients, counts.transactions) == (1, 1, 1)
    with SessionLocal() as session:
        client = session.get(Clients, 1001)
        assert client is not None
        assert client.unit_fund_id == 10
        assert client.days_since_last_activity == 22
        assert session.get(Funds, 10) is not None
        assert session.get(Transactions, 5001) is not None


def test_transform_run_is_idempotent(db: None, cleanup_runs: list[str], normalized_ids) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].add(5001)

    with SessionLocal() as session:
        _seed_run(session, run_id, _one_fund_one_client())
        transform_run(session, run_id)
        transform_run(session, run_id)

    with SessionLocal() as session:
        assert _count(session, Funds, 10, Funds.unit_fund_id) == 1
        assert _count(session, Clients, 1001, Clients.client_id) == 1
        assert _count(session, Transactions, 5001, Transactions.txn_id) == 1


def test_multi_fund_client_collapses_to_one_row_keeping_transactions(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].update({10, 20})
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].update({5001, 6001})

    payload = _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "inactive_client_count": 1,
            "clients": [_client(1001, 10, 5001)],
        },
        {
            "unit_fund_id": 20,
            "unit_fund_name": "Balanced Fund",
            "inactive_client_count": 1,
            "clients": [_client(1001, 20, 6001)],
        },
    )
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        transform_run(session, run_id)

    with SessionLocal() as session:
        assert _count(session, Clients, 1001, Clients.client_id) == 1
        txns = session.scalars(
            select(Transactions.txn_id).where(Transactions.client_id == 1001)
        ).all()
        assert set(txns) == {5001, 6001}


def test_client_name_lands_only_in_the_vault(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].add(5001)

    with SessionLocal() as session:
        _seed_run(session, run_id, _one_fund_one_client())
        counts = transform_run(session, run_id)

    assert counts.vault == 1
    with SessionLocal() as session:
        vault = session.get(PiiVault, 1001)
        assert vault is not None
        assert vault.client_name == "Jane Doe"
        # Provenance recorded; contact channels stay empty until a contact source exists.
        assert vault.source == "inactive-clients"
        assert vault.contact_email is None
        assert vault.opt_out_flag is False
    # The name exists nowhere in the normalized clients row.
    assert "client_name" not in {c.name for c in Clients.__table__.columns}


def test_retransform_updates_name_but_keeps_contact(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].add(5001)

    with SessionLocal() as session:
        _seed_run(session, run_id, _one_fund_one_client())
        transform_run(session, run_id)

    # Simulate a later contact source filling the vault.
    with SessionLocal() as session:
        vault = session.get(PiiVault, 1001)
        vault.contact_email = "jane@example.com"
        vault.opt_out_flag = True
        session.commit()

    # A re-transform must refresh the name without wiping the contact channel.
    with SessionLocal() as session:
        transform_run(session, run_id)

    with SessionLocal() as session:
        vault = session.get(PiiVault, 1001)
        assert vault.client_name == "Jane Doe"
        assert vault.contact_email == "jane@example.com"
        assert vault.opt_out_flag is True


def test_purchases_censored_persists_on_clients(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].update({1001, 2002})
    normalized_ids["txns"].update({5001, 7001, 7002, 7003, 7004, 7005})

    # One client with a single purchase, one with a full purchase window.
    full_window = {
        "client_id": 2002,
        "last_5_purchases": [
            {"id": tid, "date": "2026-07-01T00:00:00", "number": "100", "unit_fund_id": 10}
            for tid in (7001, 7002, 7003, 7004, 7005)
        ],
        "last_2_sales": [],
    }
    payload = _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "inactive_client_count": 2,
            "clients": [_client(1001, 10, 5001), full_window],
        }
    )
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        transform_run(session, run_id)

    with SessionLocal() as session:
        assert session.get(Clients, 1001).purchases_censored is False
        assert session.get(Clients, 2002).purchases_censored is True


def _count(session, model, key_value: int, key_col) -> int:
    return session.scalar(select(func.count()).select_from(model).where(key_col == key_value)) or 0
