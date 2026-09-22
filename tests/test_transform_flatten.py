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
from app.transform.features import derive_relationship_measures
from app.transform.flatten import flatten_payload, flatten_run, latest_reference_date

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


def _payload_counts(n_purchases: int, n_sales: int) -> dict[str, Any]:
    """One client carrying the given number of purchases and sales."""
    purchases = [
        {"id": i, "date": "2026-07-01T00:00:00", "number": "100", "unit_fund_id": 10}
        for i in range(n_purchases)
    ]
    sales = [
        {"id": 100 + i, "date": "2026-07-01T00:00:00", "number": "50", "unit_fund_id": 10}
        for i in range(n_sales)
    ]
    return {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 1,
                "clients": [
                    {
                        "client_id": 1001,
                        "last_5_purchases": purchases,
                        "last_2_sales": sales,
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    ("n_purchases", "n_sales", "purchases_censored", "history_censored"),
    [
        (5, 0, True, True),  # full purchase window
        (4, 0, False, False),  # room to spare
        (1, 2, False, True),  # full sale window truncates history
        (0, 0, False, False),  # nothing returned
    ],
)
def test_flatten_payload_sets_censoring_flags(
    n_purchases: int, n_sales: int, purchases_censored: bool, history_censored: bool
) -> None:
    result = flatten_payload(_payload_counts(n_purchases, n_sales), ANCHOR)
    client = result.clients[0]
    assert client.purchases_censored is purchases_censored
    assert client.history_censored is history_censored


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


def _payload_with_fund_count(client_id: int, fund_id: int, count: int) -> dict[str, Any]:
    """One fund, one client, with a given per-page headcount for that fund."""
    return {
        "data": [
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": count,
                "clients": [
                    {
                        "client_id": client_id,
                        "client_code": f"C-{client_id}",
                        "client_name": "A Name",
                        "balance": 0,
                        "computed_at": "2026-07-20T08:00:00",
                        "last_5_purchases": [],
                        "last_2_sales": [],
                    }
                ],
            }
        ]
    }


def test_flatten_payload_sums_a_repeated_fund_within_one_page() -> None:
    """A fund appearing twice in the same page's data is not a realistic shape
    the source sends, but the merge has to hold even if it did: the headcount
    adds up rather than one occurrence silently winning."""
    payload = _payload_with_fund_count(1, 10, 2)
    payload["data"].append(_payload_with_fund_count(2, 10, 3)["data"][0])

    result = flatten_payload(payload, ANCHOR)

    assert len(result.funds) == 1
    assert result.funds[0].inactive_client_count == 5


def test_flatten_run_sums_the_fund_headcount_across_pages(
    db: None, cleanup_runs: list[str]
) -> None:
    """The source reports a fund's headcount per page, not per fund. Two pages
    of the same fund must add up to the fund total, not overwrite each other."""
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
                payload=_payload_with_fund_count(1, 10, 200),
            )
        )
        session.add(
            RawStaging(
                run_id=run_id,
                endpoint="inactive-clients",
                natural_key="2",
                payload=_payload_with_fund_count(2, 10, 47),
            )
        )
        session.commit()

    with SessionLocal() as session:
        result = flatten_run(session, run_id)

    assert len(result.funds) == 1
    assert result.funds[0].inactive_client_count == 247
    assert {c.client_id for c in result.clients} == {1, 2}


def _payload_with_windows(
    last_5: list[dict[str, Any]],
    purchases_12m: list[dict[str, Any]],
    last_2: list[dict[str, Any]] | None = None,
    sales_12m: list[dict[str, Any]] | None = None,
    activity_window: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": 1,
                "clients": [
                    {
                        "client_id": 1001,
                        "last_5_purchases": last_5,
                        "last_2_sales": last_2 or [],
                        "purchases_last_12_months": purchases_12m,
                        "sales_last_12_months": sales_12m or [],
                        "activity_window": activity_window,
                    }
                ],
            }
        ]
    }


def test_a_transaction_shared_by_both_windows_is_not_double_counted() -> None:
    shared = {"id": 1, "date": "2026-06-01T00:00:00", "number": "1000", "unit_fund_id": 10}
    only_in_window = {"id": 2, "date": "2026-01-01T00:00:00", "number": "2000", "unit_fund_id": 10}
    payload = _payload_with_windows(last_5=[shared], purchases_12m=[shared, only_in_window])

    client = flatten_payload(payload, ANCHOR).clients[0]

    assert client.n_purchases_returned == 2
    assert client.total_purchase_amount == 3000


def test_purchases_censored_clears_once_the_twelve_month_window_is_present() -> None:
    last_5 = [
        {"id": i, "date": "2026-07-01T00:00:00", "number": "100", "unit_fund_id": 10}
        for i in range(5)
    ]
    payload = _payload_with_windows(last_5=last_5, purchases_12m=last_5)

    client = flatten_payload(payload, ANCHOR).clients[0]

    assert client.purchases_censored is False
    assert client.has_extended_history is True


def test_has_extended_history_is_false_without_either_new_field() -> None:
    client = flatten_payload(_payload(), ANCHOR).clients[0]
    assert client.has_extended_history is False
    assert client.activity_window_from is None
    assert client.activity_window_to is None


def test_activity_window_to_is_the_recency_anchor_when_present() -> None:
    purchase = {"id": 1, "date": "2026-01-01T00:00:00", "number": "500", "unit_fund_id": 10}
    payload = _payload_with_windows(
        last_5=[purchase],
        purchases_12m=[purchase],
        activity_window={"from": "2025-08-01", "to": "2026-07-10"},
    )

    client = flatten_payload(payload, ANCHOR).clients[0]

    assert client.activity_window_from == date(2025, 8, 1)
    assert client.activity_window_to == date(2026, 7, 10)
    assert client.last_activity_date == date(2026, 7, 10)
    assert client.days_since_last_activity == (date(2026, 7, 23) - date(2026, 7, 10)).days


def test_drawdown_ignores_fee_sized_sales_between_two_real_ones() -> None:
    sales = [
        {"id": 1, "date": "2026-01-01T00:00:00", "number": "5000", "unit_fund_id": 10},
        {"id": 2, "date": "2026-02-01T00:00:00", "number": "50", "unit_fund_id": 10},
        {"id": 3, "date": "2026-03-01T00:00:00", "number": "50", "unit_fund_id": 10},
        {"id": 4, "date": "2026-07-01T00:00:00", "number": "6000", "unit_fund_id": 10},
    ]
    payload = _payload_with_windows(
        last_5=[{"id": 99, "date": "2026-07-01T00:00:00", "number": "100", "unit_fund_id": 10}],
        purchases_12m=[],
        last_2=sales[-2:],
        sales_12m=sales,
    )

    result = flatten_payload(payload, ANCHOR)
    measures = derive_relationship_measures(result)
    measure = measures[(1001, 10)]

    assert measure.drawdown_days == (date(2026, 7, 1) - date(2026, 1, 1)).days


def test_drawdown_is_none_with_only_one_real_sale() -> None:
    sales = [
        {"id": 1, "date": "2026-01-01T00:00:00", "number": "5000", "unit_fund_id": 10},
        {"id": 2, "date": "2026-02-01T00:00:00", "number": "50", "unit_fund_id": 10},
    ]
    payload = _payload_with_windows(
        last_5=[{"id": 99, "date": "2026-07-01T00:00:00", "number": "100", "unit_fund_id": 10}],
        purchases_12m=[],
        last_2=sales,
        sales_12m=sales,
    )

    result = flatten_payload(payload, ANCHOR)
    measures = derive_relationship_measures(result)
    measure = measures[(1001, 10)]

    assert measure.drawdown_days is None


def test_latest_reference_date_reads_the_most_recently_completed_run(
    db: None, cleanup_runs: list[str]
) -> None:
    older_run = uuid4().hex
    newer_run = uuid4().hex
    still_running_run = uuid4().hex
    cleanup_runs.extend([older_run, newer_run, still_running_run])
    # Anchored far in the future (and off midnight, like ANCHOR above) so this
    # test's ordering can't be disturbed by completed runs any other test
    # happens to leave behind, or by a session-timezone round-trip shifting a
    # midnight boundary onto the wrong calendar day.
    older = datetime(2030, 1, 1, 9, 0, tzinfo=EAT)
    newer = datetime(2030, 6, 1, 9, 0, tzinfo=EAT)
    latest_but_unfinished = datetime(2030, 12, 1, 9, 0, tzinfo=EAT)
    with SessionLocal() as session:
        session.add(
            IngestionStatus(
                run_id=older_run,
                endpoint="inactive-clients",
                state="completed",
                reference_ts=older,
            )
        )
        session.add(
            IngestionStatus(
                run_id=newer_run,
                endpoint="inactive-clients",
                state="completed",
                reference_ts=newer,
            )
        )
        # A later pull that never finished must not count as the current data date.
        session.add(
            IngestionStatus(
                run_id=still_running_run,
                endpoint="inactive-clients",
                state="running",
                reference_ts=latest_but_unfinished,
            )
        )
        session.commit()

    with SessionLocal() as session:
        result = latest_reference_date(session)

    assert result == newer.date()
