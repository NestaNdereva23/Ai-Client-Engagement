"""Tests for GET /active-clients/analytics/transactions: book-wide
purchase/sale volume by month, and a sale_type breakdown among sales, both
read off active_transaction.

Each test uses its own client_id/txn_id range and cleans up after itself,
but the by_month/by_sale_type totals are book-wide, so assertions read as
deltas against a baseline taken before seeding, the same approach
test_api_risk_analytics.py uses.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import ActiveTransaction
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

CLIENT_ID = 94620
FUND_ID = 946


def _analytics(months: int = 12) -> dict:
    response = client.get(
        "/api/v1/active-clients/analytics/transactions", params={"months": months}
    )
    assert response.status_code == 200
    return response.json()


def _month_row(body: dict, month: str, txn_type: str) -> dict | None:
    return next(
        (r for r in body["by_month"] if r["month"] == month and r["txn_type"] == txn_type), None
    )


def _sale_type_count(body: dict, sale_type: str | None) -> int:
    return next((r["count"] for r in body["by_sale_type"] if r["sale_type"] == sale_type), 0)


@pytest.fixture
def cleanup():
    txn_ids: list[int] = []
    yield txn_ids
    with SessionLocal() as session:
        session.execute(delete(ActiveTransaction).where(ActiveTransaction.txn_id.in_(txn_ids)))
        session.commit()


def test_shape_has_both_sections() -> None:
    body = _analytics()
    assert isinstance(body["by_month"], list)
    assert isinstance(body["by_sale_type"], list)


def test_by_month_reflects_a_newly_recorded_purchase(cleanup) -> None:
    txn_ids = cleanup
    txn_id = 9460100001
    txn_ids.append(txn_id)
    today = date.today()
    month_key = today.replace(day=1).isoformat()

    with SessionLocal() as session:
        session.add(
            ActiveTransaction(
                txn_id=txn_id,
                txn_type="purchase",
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                txn_date=today,
                amount=12_000.0,
            )
        )
        session.commit()

    body = _analytics()
    row = _month_row(body, month_key, "purchase")
    assert row is not None
    assert row["count"] >= 1
    assert row["total_amount"] >= 12_000.0


def test_transactions_older_than_the_window_are_excluded(cleanup) -> None:
    txn_ids = cleanup
    txn_id = 9460100002
    txn_ids.append(txn_id)

    with SessionLocal() as session:
        session.add(
            ActiveTransaction(
                txn_id=txn_id,
                txn_type="purchase",
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                txn_date=date(2015, 1, 1),
                amount=999.0,
            )
        )
        session.commit()

    body = _analytics(months=1)
    row = _month_row(body, "2015-01-01", "purchase")
    assert row is None


def test_by_sale_type_reflects_a_newly_recorded_sale(cleanup) -> None:
    txn_ids = cleanup
    txn_id = 9460100003
    txn_ids.append(txn_id)
    before = _analytics()

    with SessionLocal() as session:
        session.add(
            ActiveTransaction(
                txn_id=txn_id,
                txn_type="sale",
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                txn_date=date.today(),
                amount=3_000.0,
                sale_type="active_clients_test_sale_type",
            )
        )
        session.commit()

    after = _analytics()
    assert (
        _sale_type_count(after, "active_clients_test_sale_type")
        == _sale_type_count(before, "active_clients_test_sale_type") + 1
    )
