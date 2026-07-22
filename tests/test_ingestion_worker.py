"""Tests for the ingestion worker, run against PostgreSQL.

These check the behaviour that matters for a slow, partial source: idempotent
re-runs, rejecting malformed records, keeping the raw payload as sent, resuming
after a failure, and aborting when the endpoint is not live.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.workers.ingestion import IngestionAborted, IngestionWorker


class FakeClient:
    """Stands in for CytonnClient. Paging is driven by an injected page_fetcher."""

    def __init__(self, live: bool = True):
        self.live = live

    def probe(self, path: str = "") -> bool:
        return self.live

    def fetch(self, path: str = "") -> dict:
        return {"data": []}


def _client_row(client_id=1, code="C1", amount="100", date="2025-01-01"):
    return {
        "client_id": client_id,
        "client_code": code,
        "client_name": "A Name",
        "last_5_purchases": [{"id": 9, "number": amount, "date": date}],
        "last_2_sales": [],
    }


def _page(fund_id, count, clients):
    return {
        "data": [
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": "Fund",
                "inactive_client_count": count,
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


def _counts(run_id):
    with SessionLocal() as s:
        raw = s.execute(
            text("SELECT count(*) FROM raw_staging WHERE run_id = :r"), {"r": run_id}
        ).scalar()
        rejects = s.execute(
            text("SELECT count(*) FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    return raw, rejects


def test_happy_path_counts_and_shortfall(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 5, [_client_row(1), _client_row(2)])  # count 5, returned 2 -> shortfall 3

    worker = IngestionWorker(FakeClient(), page_fetcher=_single_page(payload))
    result = worker.run(run_id=run_id)

    assert result.state == "completed"
    assert result.records_seen == 2
    assert result.records_written == 2
    assert result.records_rejected == 0
    assert result.shortfall == 3
    assert result.pages == 1


def test_malformed_record_goes_to_rejects(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    bad = {"client_code": "no-id"}  # missing client_id
    payload = _page(10, 2, [_client_row(1), bad])

    result = IngestionWorker(FakeClient(), page_fetcher=_single_page(payload)).run(run_id=run_id)

    assert result.records_seen == 2
    assert result.records_written == 1
    assert result.records_rejected == 1
    with SessionLocal() as s:
        reason = s.execute(
            text("SELECT reason FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert "client_id" in reason


def test_string_amounts_and_mixed_dates_kept_in_raw(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 1, [_client_row(1, amount="15000.50", date="2025-01-02T09:30:00+03:00")])

    result = IngestionWorker(FakeClient(), page_fetcher=_single_page(payload)).run(run_id=run_id)
    assert result.records_written == 1  # accepted, not rejected

    with SessionLocal() as s:
        stored = s.execute(
            text("SELECT payload FROM raw_staging WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    txn = stored["data"][0]["clients"][0]["last_5_purchases"][0]
    assert txn["number"] == "15000.50"
    assert txn["date"] == "2025-01-02T09:30:00+03:00"


def test_idempotent_rerun_no_dupes(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 2, [_client_row(1), {"client_code": "no-id"}])
    worker = IngestionWorker(FakeClient(), page_fetcher=_single_page(payload))

    first = worker.run(run_id=run_id)
    second = worker.run(run_id=run_id)

    assert first.records_written == second.records_written
    assert _counts(run_id) == (1, 1)  # one raw row, one reject after two runs


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

    worker = IngestionWorker(FakeClient(), page_fetcher=fetcher)

    with pytest.raises(RuntimeError):
        worker.run(run_id=run_id)
    with SessionLocal() as s:
        state_after = s.execute(
            text("SELECT state, page_cursor FROM ingestion_status WHERE run_id = :r"),
            {"r": run_id},
        ).one()
    assert state_after == ("failed", "1")

    resumed = worker.run(run_id=run_id)
    assert resumed.state == "completed"
    assert resumed.pages == 2
    assert resumed.records_written == 2
    assert _counts(run_id)[0] == 2  # two raw rows, page 1 not duplicated


def test_dead_probe_aborts_without_writing(db, cleanup_runs):
    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    payload = _page(10, 1, [_client_row(1)])

    with pytest.raises(IngestionAborted):
        IngestionWorker(FakeClient(live=False), page_fetcher=_single_page(payload)).run(
            run_id=run_id
        )

    with SessionLocal() as s:
        rows = s.execute(
            text("SELECT count(*) FROM ingestion_status WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert rows == 0
