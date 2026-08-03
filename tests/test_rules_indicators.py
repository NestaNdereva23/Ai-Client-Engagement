"""Populating client_message_indicators from the rule engine.

Seeds a small cohort through the transform, resolves it against the seeded v1
rules, and checks one traceable row per client, an idempotent re-run, and a
refresh when the resolution changes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.rules.indicators import populate_indicators

EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)
AT = date(2026, 7, 25)


def _client(cid: int, purchases: list[tuple[int, str]]) -> dict[str, Any]:
    return {
        "client_id": cid,
        "client_code": f"C-{cid}",
        "client_name": f"Client {cid}",
        "balance": 0,
        "computed_at": "2026-07-20T08:00:00",
        "last_5_purchases": [
            {"id": tid, "date": "2024-07-01T00:00:00", "number": amount, "unit_fund_id": 800}
            for (tid, amount) in purchases
        ],
        "last_2_sales": [],
    }


@pytest.fixture
def cohort(db: None, cleanup_runs: list[str]):
    """Seed a frequent high-value client and a one-and-done client, then clean up."""
    from app.transform.load import transform_run

    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    fund_id = 800
    # 80001: five purchases over the Top cutoff -> Frequent, Top.
    # 80002: a single small purchase -> One-and-done, Low.
    frequent = _client(80001, [(80100 + i, "300000") for i in range(5)])
    one_off = _client(80002, [(80200, "1000")])
    payload = {
        "data": [
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 2,
                "clients": [frequent, one_off],
            }
        ]
    }
    with SessionLocal() as session:
        session.add(
            IngestionStatus(run_id=run_id, endpoint="inactive-clients", reference_ts=ANCHOR)
        )
        session.add(
            RawStaging(run_id=run_id, endpoint="inactive-clients", natural_key="1", payload=payload)
        )
        session.commit()
        transform_run(session, run_id)

    ids = [80001, 80002]
    yield fund_id, ids

    with SessionLocal() as session:
        session.execute(
            delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id.in_(ids))
        )
        session.execute(delete(Transactions).where(Transactions.client_id.in_(ids)))
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ids)))
        session.execute(delete(ClientFund).where(ClientFund.client_id.in_(ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(ids)))
        session.execute(delete(Clients).where(Clients.client_id.in_(ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_populate_writes_one_traceable_row_per_client(cohort) -> None:
    _fund_id, ids = cohort
    with SessionLocal() as session:
        count = populate_indicators(session, at=AT)
    assert count >= 2

    with SessionLocal() as session:
        frequent = session.get(ClientMessageIndicators, 80001)
        one_off = session.get(ClientMessageIndicators, 80002)

    assert frequent.message_angle == "winback_habit"
    assert frequent.priority_tier == "P1"
    assert frequent.rule_name == "frequent_high_value"
    # The winning rule id and version are recorded for traceability.
    assert frequent.rule_id is not None
    assert frequent.rule_version == 1

    assert one_off.message_angle == "winback_flexible"
    assert one_off.rule_name == "one_and_done_default"


def test_populate_is_idempotent_keyed_by_client(cohort) -> None:
    _fund_id, ids = cohort
    with SessionLocal() as session:
        populate_indicators(session, at=AT)
        populate_indicators(session, at=AT)

    with SessionLocal() as session:
        rows = session.scalar(
            select(func.count())
            .select_from(ClientMessageIndicators)
            .where(ClientMessageIndicators.client_id.in_(ids))
        )
    assert rows == 2


def test_populate_refreshes_the_row_when_resolution_changes(cohort) -> None:
    _fund_id, ids = cohort
    with SessionLocal() as session:
        populate_indicators(session, at=AT)

    # Force a different resolution: drop the frequent client to Low value.
    with SessionLocal() as session:
        feature = session.get(ClientFeatures, 80001)
        feature.value_tier = "Low"
        session.commit()

    with SessionLocal() as session:
        populate_indicators(session, at=AT)
        refreshed = session.get(ClientMessageIndicators, 80001)

    # No longer the high-value rule; now the frequent default.
    assert refreshed.rule_name == "frequent_default"
    assert refreshed.priority_tier == "P2"


def test_populate_without_active_rules_raises() -> None:
    with SessionLocal() as session, pytest.raises(ValueError, match="no active rule"):
        populate_indicators(session, at=date(1990, 1, 1))
