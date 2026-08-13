"""Tests for the nightly risk detection worker: a full run against fixture
data, a mid-run failure leaving the prior night's snapshot untouched, and a
resume after failure that does not duplicate.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text

from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.models import PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.workers.risk_detection import RiskDetectionWorker

FUND_ID = 920
CALL_CLIENT_ID = 92001  # material balance, dormant, no rhythm -> fa_call_priority
DUST_CLIENT_ID = 92002  # dust balance, dormant -> dust_cleanup

# Well past DORMANT_DAYS (365) before any real "now" this suite runs against.
OLD_PURCHASE_DATE = "2020-01-01T00:00:00"


class FakeClient:
    """Stands in for CytonnClient. Paging is driven by an injected page_fetcher."""

    def probe(self, path: str = "") -> bool:
        return True

    def fetch(self, path: str = "", *, params: dict | None = None) -> dict:
        return {"data": []}


def _client_row(client_id: int, balance: float) -> dict:
    return {
        "client_id": client_id,
        "client_code": f"C{client_id}",
        "client_name": "Test Client",
        "balance": balance,
        "last_5_purchases": [{"id": client_id * 10, "number": "500", "date": OLD_PURCHASE_DATE}],
        "last_2_sales": [],
    }


def _payload() -> dict:
    return {
        "data": [
            {
                "unit_fund_id": FUND_ID,
                "unit_fund_name": "Fund",
                "client_count": 2,
                "clients": [
                    _client_row(CALL_CLIENT_ID, balance=200_000.0),
                    _client_row(DUST_CLIENT_ID, balance=50.0),
                ],
            }
        ]
    }


def _single_page(payload: dict):
    def fetcher(after):
        if after is not None:
            return None
        return "1", payload

    return fetcher


def _worker() -> RiskDetectionWorker:
    return RiskDetectionWorker(FakeClient(), page_fetcher=_single_page(_payload()))


@pytest.fixture
def cleanup_risk_runs():
    """Collect risk run ids (and their paired ingestion run ids, the same
    string) to delete after the test, plus the client ids the fixture uses.
    """
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        for run_id in run_ids:
            digest_run_ids = session.scalars(
                select(DigestRun.digest_run_id).where(DigestRun.risk_run_id == run_id)
            ).all()
            if digest_run_ids:
                session.execute(
                    delete(DigestLine).where(DigestLine.digest_run_id.in_(digest_run_ids))
                )
                session.execute(
                    delete(DigestRun).where(DigestRun.digest_run_id.in_(digest_run_ids))
                )
            session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
            session.execute(delete(RiskRun).where(RiskRun.run_id == run_id))
            session.execute(text("DELETE FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM raw_staging WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM ingestion_status WHERE run_id = :r"), {"r": run_id})
            session.execute(delete(AuditLog).where(AuditLog.run_id == run_id))
        client_ids = [CALL_CLIENT_ID, DUST_CLIENT_ID]
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids))
        )
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(client_ids)))
        session.commit()


def test_full_run_produces_expected_state(db, cleanup_risk_runs) -> None:
    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)

    result = _worker().run(run_id=run_id)

    assert result.state == "completed"
    assert result.clients_seen == 2
    assert result.route_distribution == {"fa_call_priority": 1, "dust_cleanup": 1}
    assert result.routes_changed == 2  # both clients are new -> both "changed" from no prior route

    with SessionLocal() as session:
        run = session.get(RiskRun, run_id)
        assert run is not None
        assert run.state == "completed"
        assert run.finished_at is not None

        snapshots = {
            row.client_id: row
            for row in session.scalars(select(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
        }
        assert set(snapshots) == {CALL_CLIENT_ID, DUST_CLIENT_ID}
        assert snapshots[CALL_CLIENT_ID].route == "fa_call_priority"
        assert snapshots[CALL_CLIENT_ID].queue_rank == 1
        assert snapshots[CALL_CLIENT_ID].sig_dormant is True
        assert snapshots[DUST_CLIENT_ID].route == "dust_cleanup"
        assert snapshots[DUST_CLIENT_ID].queue_rank is None

        features = {
            row.client_id: row
            for row in session.scalars(
                select(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id.in_([CALL_CLIENT_ID, DUST_CLIENT_ID])
                )
            )
        }
        assert features[CALL_CLIENT_ID].route == "fa_call_priority"
        assert features[DUST_CLIENT_ID].route == "dust_cleanup"

        route_audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == run_id, AuditLog.action == "route")
        )
        assert route_audit is not None
        assert route_audit.detail["changed_count"] == 2

        complete_audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == run_id, AuditLog.action == "complete")
        )
        assert complete_audit is not None


def test_second_run_is_a_no_op_result_of_completed_state(db, cleanup_risk_runs) -> None:
    """Calling run() again with the same, already-completed run id returns
    the existing summary rather than recomputing anything.
    """
    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)

    first = _worker().run(run_id=run_id)
    second = _worker().run(run_id=run_id)

    assert first.route_distribution == second.route_distribution
    assert second.clients_seen == 2


def test_mid_run_failure_leaves_prior_snapshot_untouched_and_resume_does_not_duplicate(
    db, cleanup_risk_runs, monkeypatch
) -> None:
    prior_run_id = uuid4().hex
    cleanup_risk_runs.append(prior_run_id)
    _worker().run(run_id=prior_run_id)

    with SessionLocal() as session:
        prior_snapshot_count = session.scalar(
            select(func.count())
            .select_from(RiskSnapshot)
            .where(RiskSnapshot.run_id == prior_run_id)
        )
    assert prior_snapshot_count == 2

    failing_run_id = uuid4().hex
    cleanup_risk_runs.append(failing_run_id)

    import app.workers.risk_detection as risk_detection_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(risk_detection_mod, "route_population", _boom)

    with pytest.raises(RuntimeError, match="simulated mid-run failure"):
        _worker().run(run_id=failing_run_id)

    with SessionLocal() as session:
        failed_run = session.get(RiskRun, failing_run_id)
        assert failed_run is not None
        assert failed_run.state == "failed"

        # the prior night's snapshot is untouched
        untouched_count = session.scalar(
            select(func.count())
            .select_from(RiskSnapshot)
            .where(RiskSnapshot.run_id == prior_run_id)
        )
        assert untouched_count == 2

        # nothing was written for the failed run either -- the failure was
        # before the snapshot stage committed anything
        failed_snapshot_count = session.scalar(
            select(func.count())
            .select_from(RiskSnapshot)
            .where(RiskSnapshot.run_id == failing_run_id)
        )
        assert failed_snapshot_count == 0

    monkeypatch.undo()

    resumed = _worker().run(run_id=failing_run_id)
    assert resumed.state == "completed"
    assert resumed.clients_seen == 2

    with SessionLocal() as session:
        resumed_count = session.scalar(
            select(func.count())
            .select_from(RiskSnapshot)
            .where(RiskSnapshot.run_id == failing_run_id)
        )
        assert resumed_count == 2
