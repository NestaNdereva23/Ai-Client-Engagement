"""Tests for the flatten step, focused on deterministic recency math.

The pure tests exercise flatten_payload with a fixed anchor. The database tests
prove flatten_run reads the run's persisted reference_ts, so re-running the same
run reproduces identical days_since_* rather than drifting with the wall clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.db.models.models import IngestionStatus, RawStaging
from app.db.session import SessionLocal
from app.transform.flatten import flatten_payload, flatten_run

# A fixed EAT anchor and a known last-activity date, 22 days apart.
EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)


def _payload(last_purchase: str | None = "2026-07-01T00:00:00") -> dict[str, Any]:
    """One fund with one client whose only activity is a single purchase."""
    return {
        "data": [
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
                            {"id": 1, "date": last_purchase, "number": "5000", "unit_fund_id": 10}
                        ],
                        "last_2_sales": [],
                    }
                ],
            }
        ]
    }


def test_flatten_payload_days_since_is_anchored_and_deterministic() -> None:
    first = flatten_payload(_payload(), ANCHOR)
    second = flatten_payload(_payload(), ANCHOR)

    client = first.clients[0]
    assert client.last_activity_date == date(2026, 7, 1)
    assert client.days_since_last_activity == 22
    assert client.days_since_last_activity == second.clients[0].days_since_last_activity


def test_flatten_payload_requires_a_reference_date() -> None:
    with pytest.raises(TypeError):
        flatten_payload(_payload())  # type: ignore[call-arg]


def test_flatten_run_uses_persisted_reference_ts(db: None, cleanup_runs: list[str]) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    with SessionLocal() as session:
        session.add(
            IngestionStatus(run_id=run_id, endpoint="inactive-clients", reference_ts=ANCHOR)
        )
        session.add(
            RawStaging(
                run_id=run_id,
                endpoint="inactive-clients",
                natural_key="1",
                payload=_payload(),
            )
        )
        session.commit()

    with SessionLocal() as session:
        result = flatten_run(session, run_id)

    assert result.clients[0].days_since_last_activity == 22


def test_flatten_run_is_deterministic_across_reruns(db: None, cleanup_runs: list[str]) -> None:
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    # reference_ts falls to the server default now(); both runs must still read
    # the one persisted value, not a fresh clock.
    with SessionLocal() as session:
        session.add(IngestionStatus(run_id=run_id, endpoint="inactive-clients"))
        session.add(
            RawStaging(
                run_id=run_id,
                endpoint="inactive-clients",
                natural_key="1",
                payload=_payload(),
            )
        )
        session.commit()

    with SessionLocal() as session:
        first = flatten_run(session, run_id)
    with SessionLocal() as session:
        second = flatten_run(session, run_id)

    assert first.clients == second.clients


def test_flatten_run_without_reference_ts_raises(db: None) -> None:
    with SessionLocal() as session, pytest.raises(ValueError, match="reference_ts"):
        flatten_run(session, "run-that-does-not-exist")
