"""Tests for the nightly risk detection worker: a full run against fixture
data, a mid-run failure leaving the prior night's snapshot untouched, and a
resume after failure that does not duplicate.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text

from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.digest import DigestEmailSend, DigestLine, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.models import PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.delivery.mailer import NullMailer
from app.workers.risk_detection import RiskDetectionWorker

FUND_ID = 920
CALL_CLIENT_ID = 92001  # worth-a-call balance, dormant, no pattern -> fa_call_priority
TINY_CLIENT_ID = 92002  # tiny balance, dormant -> small_balance_review

# Well past DORMANT_DAYS (365) before any real "now" this suite runs against.
OLD_DEPOSIT_DATE = "2020-01-01T00:00:00"


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
        "last_5_purchases": [{"id": client_id * 10, "number": "500", "date": OLD_DEPOSIT_DATE}],
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
                    _client_row(TINY_CLIENT_ID, balance=50.0),
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


def _delete_run_rows(session, run_id: str) -> None:
    """Everything a risk run writes, keyed by its own run_id."""
    digest_run_ids = session.scalars(
        select(DigestRun.digest_run_id).where(DigestRun.risk_run_id == run_id)
    ).all()
    if digest_run_ids:
        session.execute(
            delete(DigestEmailSend).where(DigestEmailSend.digest_run_id.in_(digest_run_ids))
        )
        session.execute(delete(DigestLine).where(DigestLine.digest_run_id.in_(digest_run_ids)))
        session.execute(delete(DigestRun).where(DigestRun.digest_run_id.in_(digest_run_ids)))
    session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
    session.execute(delete(RiskRun).where(RiskRun.run_id == run_id))
    session.execute(text("DELETE FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id})
    session.execute(text("DELETE FROM raw_staging WHERE run_id = :r"), {"r": run_id})
    session.execute(text("DELETE FROM ingestion_status WHERE run_id = :r"), {"r": run_id})
    session.execute(delete(AuditLog).where(AuditLog.run_id == run_id))


def _delete_client_rows(session, client_ids: list[int]) -> None:
    """Everything the fixture writes for a batch of client ids."""
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.execute(delete(PiiVault).where(PiiVault.client_id.in_(client_ids)))
    session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(client_ids)))


@pytest.fixture
def cleanup_risk_runs():
    """Collect risk run ids (and their paired ingestion run ids, the same
    string) to delete after the test, plus the client ids the fixture uses.
    """
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        for run_id in run_ids:
            _delete_run_rows(session, run_id)
        _delete_client_rows(session, [CALL_CLIENT_ID, TINY_CLIENT_ID])
        session.commit()


def test_full_run_produces_expected_state(db, cleanup_risk_runs) -> None:
    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)

    result = _worker().run(run_id=run_id)

    assert result.state == "completed"
    assert result.clients_seen == 2
    assert result.route_distribution == {"fa_call_priority": 1, "small_balance_review": 1}
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
        assert set(snapshots) == {CALL_CLIENT_ID, TINY_CLIENT_ID}
        assert snapshots[CALL_CLIENT_ID].route == "fa_call_priority"
        assert snapshots[CALL_CLIENT_ID].queue_rank == 1
        assert snapshots[CALL_CLIENT_ID].sig_dormant is True
        assert snapshots[TINY_CLIENT_ID].route == "small_balance_review"
        assert snapshots[TINY_CLIENT_ID].queue_rank is None

        features = {
            row.client_id: row
            for row in session.scalars(
                select(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id.in_([CALL_CLIENT_ID, TINY_CLIENT_ID])
                )
            )
        }
        assert features[CALL_CLIENT_ID].route == "fa_call_priority"
        assert features[TINY_CLIENT_ID].route == "small_balance_review"

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


@pytest.fixture
def seeded_roster(monkeypatch):
    """Two account managers in the environment, cleared again afterwards so
    no other test sees a roster.
    """
    monkeypatch.setenv(
        "ACE_FA_ROSTER",
        "71:FA Seventy One:fa71@example.com:19,72:FA Seventy Two:fa72@example.com:19",
    )
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ACE_FA_ROSTER", raising=False)
    get_settings.cache_clear()


def test_run_with_a_roster_groups_the_digest_by_a_real_advisor(
    db, cleanup_risk_runs, seeded_roster
) -> None:
    first_run = uuid4().hex
    cleanup_risk_runs.append(first_run)
    _worker().run(run_id=first_run)

    with SessionLocal() as session:
        assignments = {
            row.client_id: row.fa_id
            for row in session.scalars(
                select(FaAssignment).where(
                    FaAssignment.client_id.in_([CALL_CLIENT_ID, TINY_CLIENT_ID])
                )
            )
        }
        group_keys = set(
            session.scalars(
                select(DigestLine.group_key)
                .join(DigestRun, DigestRun.digest_run_id == DigestLine.digest_run_id)
                .where(DigestRun.risk_run_id == first_run)
            )
        )

    assert set(assignments) == {CALL_CLIENT_ID, TINY_CLIENT_ID}
    assert all(fa_id in {71, 72} for fa_id in assignments.values())
    assert group_keys == {f"fa:{assignments[CALL_CLIENT_ID]}"}

    second_run = uuid4().hex
    cleanup_risk_runs.append(second_run)
    _worker().run(run_id=second_run)

    with SessionLocal() as session:
        again = {
            row.client_id: row.fa_id
            for row in session.scalars(
                select(FaAssignment).where(
                    FaAssignment.client_id.in_([CALL_CLIENT_ID, TINY_CLIENT_ID])
                )
            )
        }

    assert again == assignments


def test_a_run_with_a_roster_mails_each_advisor_once(
    db, cleanup_risk_runs, seeded_roster, monkeypatch
) -> None:
    """The nightly run ends by mailing the roster, and mails each advisor
    exactly once for the digest it just built.
    """
    mailer = NullMailer()
    monkeypatch.setattr("app.workers.digest_email.get_mailer", lambda settings=None: mailer)

    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)
    _worker().run(run_id=run_id)

    with SessionLocal() as session:
        digest_run_id = session.scalar(
            select(DigestRun.digest_run_id).where(DigestRun.risk_run_id == run_id)
        )
        markers = session.scalars(
            select(DigestEmailSend).where(DigestEmailSend.digest_run_id == digest_run_id)
        ).all()

    assert {marker.fa_id for marker in markers} == {71, 72}
    assert {message.to for message in mailer.sent_messages} == {
        "fa71@example.com",
        "fa72@example.com",
    }


CAP_CLIENT_IDS = [92011, 92012, 92013]  # three call-eligible clients, biggest balance first


@pytest.fixture
def tight_roster(monkeypatch):
    """Two account managers with room for one call each, so the smallest of
    three call-queue clients has nowhere to be lent.
    """
    monkeypatch.setenv(
        "ACE_FA_ROSTER",
        "81:FA Eighty One:fa81@example.com:1,82:FA Eighty Two:fa82@example.com:1",
    )
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("ACE_FA_ROSTER", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def cleanup_capacity_run():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        for run_id in run_ids:
            _delete_run_rows(session, run_id)
        _delete_client_rows(session, CAP_CLIENT_IDS)
        session.commit()


def test_overflow_past_every_advisors_capacity_is_demoted_to_the_watchlist(
    db, cleanup_capacity_run, tight_roster
) -> None:
    """Two advisors with capacity for one call each cannot cover a third
    call-queue client between them, so that client's line moves to the
    watchlist route instead of piling onto its own advisor's list.
    """
    run_id = uuid4().hex
    cleanup_capacity_run.append(run_id)
    payload = {
        "data": [
            {
                "unit_fund_id": FUND_ID,
                "unit_fund_name": "Fund",
                "client_count": len(CAP_CLIENT_IDS),
                "clients": [
                    _client_row(CAP_CLIENT_IDS[0], balance=30_000.0),
                    _client_row(CAP_CLIENT_IDS[1], balance=20_000.0),
                    _client_row(CAP_CLIENT_IDS[2], balance=10_000.0),
                ],
            }
        ]
    }
    worker = RiskDetectionWorker(FakeClient(), page_fetcher=_single_page(payload))

    result = worker.run(run_id=run_id)

    assert result.route_distribution == {"fa_call_priority": 2, "fa_watchlist": 1}

    with SessionLocal() as session:
        snapshots = {
            row.client_id: row
            for row in session.scalars(select(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
        }
        owners = {
            row.client_id: row.fa_id
            for row in session.scalars(
                select(FaAssignment).where(FaAssignment.client_id.in_(CAP_CLIENT_IDS))
            )
        }

    assert snapshots[CAP_CLIENT_IDS[0]].route == "fa_call_priority"
    assert snapshots[CAP_CLIENT_IDS[1]].route == "fa_call_priority"
    assert snapshots[CAP_CLIENT_IDS[2]].route == "fa_watchlist"
    assert snapshots[CAP_CLIENT_IDS[2]].queue_rank is None
    # Demotion never touches ownership -- the client keeps a real advisor.
    assert owners[CAP_CLIENT_IDS[2]] in {81, 82}
