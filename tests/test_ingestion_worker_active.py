"""Tests for the active-clients feed through IngestionWorker.

Same worker as the dormant feed, pointed at the active-clients contracts via
app.ingestion.endpoints.resolve_endpoint. These check the behaviour specific
to that feed: the client_count reconciliation field, sale_type passing
through raw staging untouched, and that idempotency and resume still hold.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal
from app.ingestion.endpoints import resolve_endpoint
from app.workers.ingestion import IngestionAborted, IngestionWorker


class FakeClient:
    """Stands in for CytonnClient. Paging is driven by an injected page_fetcher."""

    def __init__(self, live: bool = True):
        self.live = live

    def probe(self, path: str = "") -> bool:
        return self.live

    def fetch(self, path: str = "") -> dict:
        return {"data": []}


def _worker(client, page_fetcher) -> IngestionWorker:
    config = resolve_endpoint("active-clients", get_settings())
    return IngestionWorker(
        client,
        endpoint="active-clients",
        fetch_path=config.fetch_path,
        page_fetcher=page_fetcher,
        fund_model=config.fund_model,
        client_model=config.client_model,
        schema_drift_fn=config.schema_drift_fn,
        count_field=config.count_field,
    )


def _client_row(client_id=1, balance=15000.0, amount="100", date="2025-01-01", sale_type=None):
    return {
        "client_id": client_id,
        "client_code": "C1",
        "client_name": "A Name",
        "balance": balance,
        "last_5_purchases": [{"id": 9, "number": amount, "date": date}],
        "last_2_sales": (
            [{"id": 10, "number": "50", "date": date, "sale_type": sale_type}] if sale_type else []
        ),
    }


def _page(fund_id, count, clients):
    return {
        "data": [
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": "Fund",
                "client_count": count,
                "clients": clients,
            }
        ]
    }


def _single_page(payload):
    def fetcher(after):
        if after is not None:
            return None
        return "1", payload

    return fetcher


def test_reconciliation_uses_client_count(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 5, [_client_row(1), _client_row(2)])  # count 5, returned 2 -> shortfall 3

    result = _worker(FakeClient(), _single_page(payload)).run(run_id=run_id)

    assert result.state == "completed"
    assert result.records_seen == 2
    assert result.records_written == 2
    assert result.shortfall == 3


def test_sale_type_kept_in_raw(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 1, [_client_row(1, sale_type="withdrawal")])

    result = _worker(FakeClient(), _single_page(payload)).run(run_id=run_id)
    assert result.records_written == 1

    with SessionLocal() as s:
        stored = s.execute(
            text("SELECT payload FROM raw_staging WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    sale = stored["data"][0]["clients"][0]["last_2_sales"][0]
    assert sale["sale_type"] == "withdrawal"


def test_malformed_record_goes_to_rejects(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    bad = {"client_code": "no-id"}  # missing client_id
    payload = _page(10, 2, [_client_row(1), bad])

    result = _worker(FakeClient(), _single_page(payload)).run(run_id=run_id)

    assert result.records_seen == 2
    assert result.records_written == 1
    assert result.records_rejected == 1


def test_idempotent_rerun_no_dupes(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 1, [_client_row(1)])
    worker = _worker(FakeClient(), _single_page(payload))

    first = worker.run(run_id=run_id)
    second = worker.run(run_id=run_id)

    assert first.records_written == second.records_written == 1
    with SessionLocal() as s:
        raw_rows = s.execute(
            text("SELECT count(*) FROM raw_staging WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert raw_rows == 1


def test_resume_after_failure(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    state = {"fail_page_2": True}

    def fetcher(after):
        if after is None:
            return "1", _page(10, 1, [_client_row(1)])
        if after == "1":
            if state["fail_page_2"]:
                state["fail_page_2"] = False
                raise RuntimeError("boom on page 2")
            return "2", _page(20, 1, [_client_row(2)])
        return None

    worker = _worker(FakeClient(), fetcher)

    with pytest.raises(RuntimeError):
        worker.run(run_id=run_id)

    resumed = worker.run(run_id=run_id)
    assert resumed.state == "completed"
    assert resumed.pages == 2
    assert resumed.records_written == 2


def test_dead_probe_aborts_without_writing(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 1, [_client_row(1)])

    with pytest.raises(IngestionAborted):
        _worker(FakeClient(live=False), _single_page(payload)).run(run_id=run_id)

    with SessionLocal() as s:
        rows = s.execute(
            text("SELECT count(*) FROM ingestion_status WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert rows == 0
