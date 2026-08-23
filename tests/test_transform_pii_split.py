from __future__ import annotations

import dataclasses
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select

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
from app.transform.features import FeatureRow, derive_features
from app.transform.flatten import flatten_payload

EAT = timezone(timedelta(hours=3))
# Anchored well after every generated date so recency math never underflows.
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)

# Restricted fields that must never leave pii_vault, matched against column
# names and against persisted string values.
PII_COLUMNS = {"client_name", "contact_email", "contact_whatsapp", "opt_out_flag"}
NON_VAULT_MODELS = [Funds, Clients, ClientFund, Transactions, ClientFeatures]


def _txn(rng: random.Random, txn_id: int, fund_id: int) -> dict[str, Any]:
    """One transaction with a random date and a mix of clean and dirty amounts."""
    day = rng.randint(1, 28)
    month = rng.randint(1, 12)
    year = rng.choice([2019, 2021, 2023, 2024, 2025])
    # Occasionally an unparseable amount, to exercise the lenient parse path;
    # it must not make the derivation drift on a rerun.
    number = rng.choice(["100", "5000", "250000", "1000000", "not-a-number", ""])
    return {
        "id": txn_id,
        "date": f"{year:04d}-{month:02d}-{day:02d}T00:00:00",
        "number": number,
        "unit_fund_id": fund_id,
    }


def _random_payload(seed: int) -> dict[str, Any]:
    """Build a varied but well-formed payload deterministically from a seed.

    Funds hold overlapping clients so multi-fund aggregation is exercised, and
    purchase and sale counts span the censoring boundaries on both windows.
    """
    rng = random.Random(seed)
    txn_id = seed * 10_000
    funds = []
    for f in range(rng.randint(1, 3)):
        fund_id = 10 + f
        clients = []
        for _ in range(rng.randint(1, 4)):
            client_id = 1000 + rng.randint(0, 5)  # collisions across funds are intended
            n_purchases = rng.randint(0, 6)
            n_sales = rng.randint(0, 3)
            purchases = []
            for _ in range(n_purchases):
                txn_id += 1
                purchases.append(_txn(rng, txn_id, fund_id))
            sales = []
            for _ in range(n_sales):
                txn_id += 1
                sales.append(_txn(rng, txn_id, fund_id))
            clients.append(
                {
                    "client_id": client_id,
                    "client_code": f"C-{client_id}",
                    "client_name": f"Client {client_id}",
                    "balance": 0,
                    "computed_at": "2026-07-20T08:00:00",
                    "last_5_purchases": purchases,
                    "last_2_sales": sales,
                }
            )
        funds.append(
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": f"Fund {fund_id}",
                "inactive_client_count": len(clients),
                "clients": clients,
            }
        )
    return {"data": funds}


@pytest.mark.parametrize("seed", range(25))
def test_same_payload_and_anchor_derive_identically(seed: int) -> None:
    payload = _random_payload(seed)
    first = flatten_payload(payload, ANCHOR)
    second = flatten_payload(payload, ANCHOR)

    assert first.funds == second.funds
    assert first.clients == second.clients
    assert first.transactions == second.transactions
    assert derive_features(first) == derive_features(second)


def test_feature_row_carries_no_pii_field() -> None:
    """The model-facing feature row exposes no name or contact field at all."""
    names = {f.name for f in dataclasses.fields(FeatureRow)}
    assert names.isdisjoint(PII_COLUMNS)


@pytest.mark.parametrize("model", NON_VAULT_MODELS)
def test_normalized_tables_have_no_pii_columns(model: type) -> None:
    columns = {c.name for c in model.__table__.columns}
    assert columns.isdisjoint(PII_COLUMNS), f"{model.__tablename__} exposes a PII column"


def test_only_pii_vault_declares_pii_columns() -> None:
    assert PII_COLUMNS <= {c.name for c in PiiVault.__table__.columns}


CENSORING_CASES = [
    # (purchases, sales) -> (purchases_censored, history_censored)
    ((5, 0), (True, True)),  # full purchase window
    ((4, 0), (False, False)),  # room to spare
    ((1, 2), (False, True)),  # full sale window truncates history
    ((0, 0), (False, False)),  # nothing observed
]


@pytest.mark.parametrize(("counts", "expected"), CENSORING_CASES)
def test_censoring_flags_survive_flatten_to_features(
    counts: tuple[int, int], expected: tuple[bool, bool]
) -> None:
    n_purchases, n_sales = counts
    purchases = [
        {"id": i, "date": "2024-01-01T00:00:00", "number": "100", "unit_fund_id": 10}
        for i in range(n_purchases)
    ]
    sales = [
        {"id": 100 + i, "date": "2024-01-01T00:00:00", "number": "50", "unit_fund_id": 10}
        for i in range(n_sales)
    ]
    payload = {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 1,
                "clients": [
                    {"client_id": 1001, "last_5_purchases": purchases, "last_2_sales": sales}
                ],
            }
        ]
    }
    result = flatten_payload(payload, ANCHOR)
    client = result.clients[0]
    features = derive_features(result)
    assert len(features) == 1
    # The flag set on the flattened client row is the flag carried to the feature.
    assert (client.purchases_censored, client.history_censored) == expected
    assert (features[0].purchases_censored, features[0].history_censored) == expected


def test_censoring_survives_multi_fund_aggregation() -> None:
    """A client censored in one fund is censored in its single feature row."""

    def _client(fund_id: int, base: int, n_purchases: int) -> dict[str, Any]:
        return {
            "client_id": 1001,
            "last_5_purchases": [
                {
                    "id": base + i,
                    "date": "2024-01-01T00:00:00",
                    "number": "100",
                    "unit_fund_id": fund_id,
                }
                for i in range(n_purchases)
            ],
            "last_2_sales": [],
        }

    payload = {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Fund A",
                "inactive_client_count": 1,
                "clients": [_client(10, 100, 1)],  # not censored here
            },
            {
                "unit_fund_id": 20,
                "unit_fund_name": "Fund B",
                "inactive_client_count": 1,
                "clients": [_client(20, 200, 5)],  # full window here
            },
        ]
    }
    features = derive_features(flatten_payload(payload, ANCHOR))
    assert len(features) == 1
    assert features[0].purchases_censored is True
    assert features[0].history_censored is True


def _seed_run(session, run_id: str, payload: dict[str, Any]) -> None:
    session.add(IngestionStatus(run_id=run_id, endpoint="inactive-clients", reference_ts=ANCHOR))
    session.add(
        RawStaging(run_id=run_id, endpoint="inactive-clients", natural_key="1", payload=payload)
    )
    session.commit()


def test_client_name_appears_in_no_persisted_row_outside_the_vault(
    db: None, cleanup_runs: list[str]
) -> None:
    """After a real transform, scan every non-vault row for the name string."""
    from app.transform.load import transform_run

    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    secret_name = "Wangari Distinctive-Name"
    payload = {
        "data": [
            {
                "unit_fund_id": 910,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 1,
                "clients": [
                    {
                        "client_id": 91001,
                        "client_code": "C-91001",
                        "client_name": secret_name,
                        "balance": 0,
                        "computed_at": "2026-07-20T08:00:00",
                        "last_5_purchases": [
                            {
                                "id": 95001,
                                "date": "2024-07-01T00:00:00",
                                "number": "5000",
                                "unit_fund_id": 910,
                            }
                        ],
                        "last_2_sales": [],
                    }
                ],
            }
        ]
    }

    try:
        with SessionLocal() as session:
            _seed_run(session, run_id, payload)
            transform_run(session, run_id)

        with SessionLocal() as session:
            # The name is present in the vault.
            assert session.get(PiiVault, 91001).client_name == secret_name
            # And absent from every column of every non-vault row for this client.
            for model in NON_VAULT_MODELS:
                rows = session.scalars(select(model)).all()
                for row in rows:
                    values = [str(getattr(row, c.name)) for c in inspect(model).columns]
                    assert secret_name not in " | ".join(values)
    finally:
        with SessionLocal() as session:
            session.execute(Transactions.__table__.delete().where(Transactions.client_id == 91001))
            session.execute(
                ClientFeatures.__table__.delete().where(ClientFeatures.client_id == 91001)
            )
            session.execute(ClientFund.__table__.delete().where(ClientFund.client_id == 91001))
            session.execute(PiiVault.__table__.delete().where(PiiVault.client_id == 91001))
            session.execute(Clients.__table__.delete().where(Clients.client_id == 91001))
            session.execute(Funds.__table__.delete().where(Funds.unit_fund_id == 910))
            session.commit()


def _client_payload(fund_id: int, client_id: int, **overrides: Any) -> dict[str, Any]:
    client = {
        "client_id": client_id,
        "client_code": f"C-{client_id}",
        "client_name": "Test Client",
        "balance": 0,
        "computed_at": "2026-07-20T08:00:00",
        "last_5_purchases": [
            {
                "id": client_id * 10 + 1,
                "date": "2024-07-01T00:00:00",
                "number": "5000",
                "unit_fund_id": fund_id,
            }
        ],
        "last_2_sales": [],
    }
    client.update(overrides)
    return {
        "data": [
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 1,
                "clients": [client],
            }
        ]
    }


def test_client_email_and_phone_land_in_pii_vault(db: None, cleanup_runs: list[str]) -> None:
    from app.transform.load import transform_run

    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    fund_id, client_id = 920, 92001
    payload = _client_payload(
        fund_id, client_id, client_email="wangari@example.com", client_phone="+254700000001"
    )

    try:
        with SessionLocal() as session:
            _seed_run(session, run_id, payload)
            transform_run(session, run_id)

        with SessionLocal() as session:
            vault = session.get(PiiVault, client_id)
            assert vault.contact_email == "wangari@example.com"
            assert vault.contact_whatsapp == "+254700000001"
    finally:
        with SessionLocal() as session:
            session.execute(
                Transactions.__table__.delete().where(Transactions.client_id == client_id)
            )
            session.execute(
                ClientFeatures.__table__.delete().where(ClientFeatures.client_id == client_id)
            )
            session.execute(ClientFund.__table__.delete().where(ClientFund.client_id == client_id))
            session.execute(PiiVault.__table__.delete().where(PiiVault.client_id == client_id))
            session.execute(Clients.__table__.delete().where(Clients.client_id == client_id))
            session.execute(Funds.__table__.delete().where(Funds.unit_fund_id == fund_id))
            session.commit()


def test_retransform_with_no_contact_keeps_previously_known_contact(
    db: None, cleanup_runs: list[str]
) -> None:
    """A rerun whose page carries no email or phone for a client must not blank
    out a contact already on file, whether that contact came from an earlier
    ingestion run or from a manual /integration/contacts push."""
    from app.transform.load import transform_run

    run_id_1, run_id_2 = uuid4().hex, uuid4().hex
    cleanup_runs.extend([run_id_1, run_id_2])
    fund_id, client_id = 920, 92002
    first_payload = _client_payload(
        fund_id, client_id, client_email="first@example.com", client_phone="+254700000002"
    )
    second_payload = _client_payload(fund_id, client_id, client_email=None, client_phone=None)

    try:
        with SessionLocal() as session:
            _seed_run(session, run_id_1, first_payload)
            transform_run(session, run_id_1)
            _seed_run(session, run_id_2, second_payload)
            transform_run(session, run_id_2)

        with SessionLocal() as session:
            vault = session.get(PiiVault, client_id)
            assert vault.contact_email == "first@example.com"
            assert vault.contact_whatsapp == "+254700000002"
    finally:
        with SessionLocal() as session:
            session.execute(
                Transactions.__table__.delete().where(Transactions.client_id == client_id)
            )
            session.execute(
                ClientFeatures.__table__.delete().where(ClientFeatures.client_id == client_id)
            )
            session.execute(ClientFund.__table__.delete().where(ClientFund.client_id == client_id))
            session.execute(PiiVault.__table__.delete().where(PiiVault.client_id == client_id))
            session.execute(Clients.__table__.delete().where(Clients.client_id == client_id))
            session.execute(Funds.__table__.delete().where(Funds.unit_fund_id == fund_id))
            session.commit()
