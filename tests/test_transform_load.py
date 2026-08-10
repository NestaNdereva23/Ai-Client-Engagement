from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.db.models.models import (
    ClientFeatures,
    ClientFund,
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


def _client(client_id: int, fund_id: int, txn_id: int, amount: str = "5000") -> dict[str, Any]:
    return {
        "client_id": client_id,
        "client_code": "C-1",
        "client_name": "Jane Doe",
        "balance": 0,
        "computed_at": "2026-07-20T08:00:00",
        "last_5_purchases": [
            {"id": txn_id, "date": "2026-07-01T00:00:00", "number": amount, "unit_fund_id": fund_id}
        ],
        "last_2_sales": [],
    }


def _two_funds(small: str, large: str) -> dict[str, Any]:
    """One client in two funds, fund 10 buying `small` and fund 20 buying `large`."""
    return _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "inactive_client_count": 1,
            "clients": [_client(1001, 10, 5001, small)],
        },
        {
            "unit_fund_id": 20,
            "unit_fund_name": "Balanced Fund",
            "inactive_client_count": 1,
            "clients": [_client(1001, 20, 6001, large)],
        },
    )


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
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ids["clients"])))
        session.execute(delete(ClientFund).where(ClientFund.client_id.in_(ids["clients"])))
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


@pytest.fixture
def two_fund_client(db: None, cleanup_runs: list[str], normalized_ids):
    """Transform one client holding two funds, with the amounts the test names."""

    def run(small: str = "5000", large: str = "90000") -> None:
        run_id = uuid4().hex
        cleanup_runs.append(run_id)
        normalized_ids["funds"].update({10, 20})
        normalized_ids["clients"].add(1001)
        normalized_ids["txns"].update({5001, 6001})
        with SessionLocal() as session:
            _seed_run(session, run_id, _two_funds(small, large))
            transform_run(session, run_id)

    return run


def _funds_held(session, client_id: int) -> list[ClientFund]:
    return list(
        session.scalars(
            select(ClientFund)
            .where(ClientFund.client_id == client_id)
            .order_by(ClientFund.unit_fund_id)
        ).all()
    )


def test_a_client_in_two_funds_keeps_both_relationships(two_fund_client) -> None:
    two_fund_client()

    with SessionLocal() as session:
        held = _funds_held(session, 1001)
        assert [row.unit_fund_id for row in held] == [10, 20]
        assert [row.observed_volume for row in held] == [5000, 90000]


def test_a_client_in_two_funds_is_still_contacted_once(two_fund_client) -> None:
    two_fund_client()

    with SessionLocal() as session:
        assert _count(session, Clients, 1001, Clients.client_id) == 1
        primary = [r.unit_fund_id for r in _funds_held(session, 1001) if r.is_primary_contact_row]
        assert primary == [20]


def test_the_person_row_comes_from_the_largest_relationship(two_fund_client) -> None:
    two_fund_client(small="5000", large="90000")

    with SessionLocal() as session:
        client = session.get(Clients, 1001)
        assert client.unit_fund_id == 20
        assert client.total_purchase_amount == 90000
        assert client.n_funds == 2


def test_the_smaller_fund_wins_nothing_whichever_order_it_arrives(two_fund_client) -> None:
    """Fund 10 is the larger here, so it must win despite arriving first."""
    two_fund_client(small="90000", large="5000")

    with SessionLocal() as session:
        client = session.get(Clients, 1001)
        assert client.unit_fund_id == 10
        primary = [r.unit_fund_id for r in _funds_held(session, 1001) if r.is_primary_contact_row]
        assert primary == [10]


def test_an_equal_tie_picks_the_same_relationship_every_time(two_fund_client) -> None:
    two_fund_client(small="5000", large="5000")

    with SessionLocal() as session:
        assert session.get(Clients, 1001).unit_fund_id == 10


def test_transactions_from_every_fund_survive(two_fund_client) -> None:
    two_fund_client()

    with SessionLocal() as session:
        txns = session.scalars(
            select(Transactions.txn_id).where(Transactions.client_id == 1001)
        ).all()
        assert set(txns) == {5001, 6001}


def test_re_transforming_does_not_duplicate_relationships(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].update({10, 20})
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].update({5001, 6001})

    with SessionLocal() as session:
        _seed_run(session, run_id, _two_funds("5000", "90000"))
        transform_run(session, run_id)
        counts = transform_run(session, run_id)

    assert (counts.clients, counts.client_funds) == (1, 2)
    with SessionLocal() as session:
        assert len(_funds_held(session, 1001)) == 2


def test_a_single_fund_client_holds_one_relationship(
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

    assert counts.client_funds == 1
    with SessionLocal() as session:
        (only,) = _funds_held(session, 1001)
        assert only.is_primary_contact_row is True
        assert session.get(Clients, 1001).n_funds == 1


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


def test_transform_run_persists_client_features(
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

    assert counts.features == 1
    with SessionLocal() as session:
        feature = session.get(ClientFeatures, 1001)
        assert feature is not None
        assert feature.history_censored is False
        assert feature.value_band in {"Low", "Medium", "High", "Top"}


def _measured_client() -> dict[str, Any]:
    """Three rising contributions a month apart, withdrawn 400 days later.

    Chosen so every measure lands on a value worth asserting rather than on a
    default: a tight cadence, a clearly rising trend, and money that stayed a
    year or more after the final top-up.
    """
    return _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "inactive_client_count": 1,
            "clients": [
                {
                    "client_id": 1001,
                    "client_code": "C-1",
                    "client_name": "Jane Doe",
                    "balance": 0,
                    "computed_at": "2026-07-20T08:00:00",
                    "last_5_purchases": [
                        {"id": 5001, "date": "2022-01-01", "number": "100000", "unit_fund_id": 10},
                        {"id": 5002, "date": "2022-01-31", "number": "1000000", "unit_fund_id": 10},
                        {
                            "id": 5003,
                            "date": "2022-03-02",
                            "number": "10000000",
                            "unit_fund_id": 10,
                        },
                    ],
                    "last_2_sales": [
                        {
                            "id": 5004,
                            "date": "2023-04-06",
                            "number": "111000",
                            "unit_fund_id": 10,
                            "sale_type": "unit_sale",
                        }
                    ],
                }
            ],
        }
    )


@pytest.fixture
def measured(db: None, cleanup_runs: list[str], normalized_ids):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].update({5001, 5002, 5003, 5004})
    with SessionLocal() as session:
        _seed_run(session, run_id, _measured_client())
        transform_run(session, run_id)
    return run_id


def test_relationship_measures_persist(measured) -> None:
    with SessionLocal() as session:
        (row,) = _funds_held(session, 1001)
        assert row.avg_ticket == 3700000
        assert row.max_ticket == 10000000
        assert row.rhythm_days == 30
        assert row.active_window_days == 60
        assert row.hold_days == 400
        assert row.drawdown_days == 0
        assert row.exit_type == "unit_sale"
        assert row.ticket_trend is not None and row.ticket_trend > 0


def test_bands_persist_on_features(measured) -> None:
    with SessionLocal() as session:
        f = session.get(ClientFeatures, 1001)
        assert f.hold_band == "Stayed years"
        assert f.cadence_band == "Tight"
        assert f.trend_band == "rising"
        assert f.value_band == "High"
        assert f.purchase_depth == "few"
        assert f.fund_type == "money_market"
        assert f.exit_reason == "client_sale"
        assert f.has_depth is True
        assert f.in_wave is False
        assert f.staged_exit is False
        assert f.stale_contact is True
        assert f.holds_other_funds is False
        assert f.n_funds == 1


def test_re_deriving_gives_the_same_measures(
    db: None, cleanup_runs: list[str], normalized_ids
) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].add(1001)
    normalized_ids["txns"].update({5001, 5002, 5003, 5004})

    with SessionLocal() as session:
        _seed_run(session, run_id, _measured_client())
        transform_run(session, run_id)
    with SessionLocal() as session:
        (first,) = _funds_held(session, 1001)
        before = (first.avg_ticket, first.rhythm_days, first.ticket_trend, first.hold_days)

    with SessionLocal() as session:
        transform_run(session, run_id)
    with SessionLocal() as session:
        (second,) = _funds_held(session, 1001)
        assert (second.avg_ticket, second.rhythm_days, second.ticket_trend, second.hold_days) == (
            before
        )


def test_upsert_batches_to_stay_under_the_postgres_bind_param_limit(
    db: None, cleanup_runs: list[str], normalized_ids, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the real limit (65535): force tiny batches with a
    handful of rows instead of needing thousands of real clients to trigger it.
    """
    from app.transform import load as load_module

    monkeypatch.setattr(load_module, "_MAX_BIND_PARAMS", 30)  # forces several batches

    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    n_clients = 12
    client_ids = list(range(9001, 9001 + n_clients))
    normalized_ids["funds"].add(10)
    normalized_ids["clients"].update(client_ids)
    normalized_ids["txns"].update(range(90001, 90001 + n_clients))

    clients = [_client(cid, 10, 90001 + i) for i, cid in enumerate(client_ids)]
    payload = _payload(
        {
            "unit_fund_id": 10,
            "unit_fund_name": "Money Market Fund",
            "inactive_client_count": n_clients,
            "clients": clients,
        }
    )
    with SessionLocal() as session:
        _seed_run(session, run_id, payload)
        counts = transform_run(session, run_id)

    assert counts.clients == n_clients
    with SessionLocal() as session:
        rows = session.scalars(select(Clients).where(Clients.client_id.in_(client_ids))).all()
        assert {row.client_id for row in rows} == set(client_ids)


def _count(session, model, key_value: int, key_col) -> int:
    return session.scalar(select(func.count()).select_from(model).where(key_col == key_value)) or 0
