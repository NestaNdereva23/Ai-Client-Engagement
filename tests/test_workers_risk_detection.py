"""Tests for the nightly risk detection worker: a full run against fixture
data, a mid-run failure leaving the prior night's snapshot untouched, and a
resume after failure that does not duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text

from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.digest import DigestEmailSend, DigestLine, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.models import PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.models.routing import ClientTier
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.models.user_access import ROLE_ADVISOR, UserAccess
from app.db.session import SessionLocal
from app.delivery.mailer import NullMailer
from app.workers import risk_detection
from app.workers.risk_detection import RiskDetectionWorker

FUND_ID = 920
CALL_CLIENT_ID = 92001  # scored: balance above the preservation line, dormant, no pattern
TINY_CLIENT_ID = 92002  # below the preservation line, so held out of scoring

# Well past DORMANT_DAYS (365) before any real "now" this suite runs against.
OLD_DEPOSIT_DATE = "2020-01-01T00:00:00"


class FakeClient:
    """Stands in for CytonnClient. Paging is driven by an injected page_fetcher."""

    def probe(self, path: str = "") -> bool:
        return True

    def fetch(self, path: str = "", *, params: dict | None = None) -> dict:
        return {"data": []}


def _client_row(client_id: int, balance: float, deposit_date: str = OLD_DEPOSIT_DATE) -> dict:
    return {
        "client_id": client_id,
        "client_code": f"C{client_id}",
        "client_name": "Test Client",
        "balance": balance,
        "last_5_purchases": [{"id": client_id * 10, "number": "500", "date": deposit_date}],
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


def _delete_agent_proposal_rows(session, client_ids: list[int]) -> None:
    """Any agent proposals written for these clients, in case a test turns
    the agent on for a run instead of mocking it.
    """
    proposal_ids = session.scalars(
        select(AgentProposalClient.proposal_id)
        .where(AgentProposalClient.client_id.in_(client_ids))
        .distinct()
    ).all()
    if not proposal_ids:
        return
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "agent_proposal",
            AuditLog.entity_id.in_([str(proposal_id) for proposal_id in proposal_ids]),
        )
    )
    session.execute(
        delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids))
    )
    session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(proposal_ids)))


def _delete_client_rows(session, client_ids: list[int]) -> None:
    """Everything the fixture writes for a batch of client ids."""
    _delete_agent_proposal_rows(session, client_ids)
    for signal_model in (
        ClientSituationSnapshot,
        ClientSituationState,
        ClientSignalSnapshot,
        ClientSignalState,
    ):
        session.execute(delete(signal_model).where(signal_model.client_id.in_(client_ids)))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids)))
    session.execute(delete(ClientTier).where(ClientTier.client_id.in_(client_ids)))
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
    assert result.clients_seen == 1
    assert result.route_distribution == {"fa_action": 1}
    assert result.routes_changed == 1

    with SessionLocal() as session:
        run = session.get(RiskRun, run_id)
        assert run is not None
        assert run.state == "completed"
        assert run.finished_at is not None

        snapshots = {
            row.client_id: row
            for row in session.scalars(select(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
        }
        assert set(snapshots) == {CALL_CLIENT_ID}
        assert snapshots[CALL_CLIENT_ID].route == "fa_action"
        assert snapshots[CALL_CLIENT_ID].queue_rank is None
        assert snapshots[CALL_CLIENT_ID].sig_dormant is True

        features = {
            row.client_id: row
            for row in session.scalars(
                select(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id.in_([CALL_CLIENT_ID, TINY_CLIENT_ID])
                )
            )
        }
        assert features[CALL_CLIENT_ID].route == "fa_action"
        assert TINY_CLIENT_ID not in features

        route_audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == run_id, AuditLog.action == "route")
        )
        assert route_audit is not None
        assert route_audit.detail["changed_count"] == 1

        complete_audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == run_id, AuditLog.action == "complete")
        )
        assert complete_audit is not None


def test_the_run_writes_a_tier_row_for_every_client_in_the_feed(db, cleanup_risk_runs) -> None:
    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)

    _worker().run(run_id=run_id)

    with SessionLocal() as session:
        tiers = {
            row.client_id: row.tier
            for row in session.scalars(
                select(ClientTier).where(ClientTier.client_id.in_([CALL_CLIENT_ID, TINY_CLIENT_ID]))
            )
        }
        assert tiers == {
            CALL_CLIENT_ID: "no_transactions_in_review_period",
            TINY_CLIENT_ID: "below_capital_preservation_threshold",
        }
        audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == run_id, AuditLog.entity_type == "client_tier")
        )
        assert audit is not None
        assert audit.detail["clients"] == 2


def test_a_failed_tier_write_does_not_fail_the_run(db, cleanup_risk_runs, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("tier write failed")

    monkeypatch.setattr(risk_detection, "write_client_tiers", boom)
    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)

    result = _worker().run(run_id=run_id)

    assert result.state == "completed"


def test_second_run_is_a_no_op_result_of_completed_state(db, cleanup_risk_runs) -> None:
    """Calling run() again with the same, already-completed run id returns
    the existing summary rather than recomputing anything.
    """
    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)

    first = _worker().run(run_id=run_id)
    second = _worker().run(run_id=run_id)

    assert first.route_distribution == second.route_distribution
    assert second.clients_seen == 1


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
    assert prior_snapshot_count == 1

    failing_run_id = uuid4().hex
    cleanup_risk_runs.append(failing_run_id)

    import app.workers.risk_detection as risk_detection_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(risk_detection_mod, "route_for_active_client", _boom)

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
        assert untouched_count == 1

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
    assert resumed.clients_seen == 1

    with SessionLocal() as session:
        resumed_count = session.scalar(
            select(func.count())
            .select_from(RiskSnapshot)
            .where(RiskSnapshot.run_id == failing_run_id)
        )
        assert resumed_count == 1


def _delete_advisors() -> None:
    with SessionLocal() as session:
        session.execute(delete(UserAccess).where(UserAccess.role == ROLE_ADVISOR))
        session.commit()


@pytest.fixture(autouse=True)
def _no_leftover_advisors(db):
    """Advisors are read from user_access, and the run adds the ones it finds in
    the feed, so every test starts and ends with none."""
    _delete_advisors()
    yield
    _delete_advisors()


@pytest.fixture
def seeded_roster():
    """Two account managers in user_access, both switched on for the digest."""
    with SessionLocal() as session:
        session.add_all(
            [
                UserAccess(
                    email=f"fa{number}@example.com",
                    role=ROLE_ADVISOR,
                    fa_id=f"fa-{number}",
                    name=f"FA Seventy {number}",
                    receives_digest=True,
                )
                for number in (71, 72)
            ]
        )
        session.commit()


def test_run_with_a_roster_gives_every_scored_client_a_real_advisor_and_keeps_them(
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

    assert set(assignments) == {CALL_CLIENT_ID}
    assert all(fa_id in {"fa-71", "fa-72"} for fa_id in assignments.values())

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
    """The nightly run ends by mailing the advisors switched on for the digest,
    and mails each exactly once for the digest it just built.
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
        owner = session.scalar(
            select(FaAssignment.fa_id).where(FaAssignment.client_id == CALL_CLIENT_ID)
        )

    assert owner in {"fa-71", "fa-72"}
    assert {marker.fa_id for marker in markers} == {owner}
    assert [message.to for message in mailer.sent_messages] == [
        f"{owner.replace('-', '')}@example.com"
    ]


BAND_CLIENT_IDS = [92011, 92012]


@pytest.fixture
def cleanup_band_run():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        for run_id in run_ids:
            _delete_run_rows(session, run_id)
        _delete_client_rows(session, BAND_CLIENT_IDS)
        session.commit()


def test_every_scored_client_goes_to_fa_action_whatever_their_band(db, cleanup_band_run) -> None:
    run_id = uuid4().hex
    cleanup_band_run.append(run_id)
    recent_deposit = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
    payload = {
        "data": [
            {
                "unit_fund_id": FUND_ID,
                "unit_fund_name": "Fund",
                "client_count": len(BAND_CLIENT_IDS),
                "clients": [
                    _client_row(BAND_CLIENT_IDS[0], balance=30_000.0),
                    _client_row(BAND_CLIENT_IDS[1], balance=30_000.0, deposit_date=recent_deposit),
                ],
            }
        ]
    }
    worker = RiskDetectionWorker(FakeClient(), page_fetcher=_single_page(payload))

    result = worker.run(run_id=run_id)

    assert result.route_distribution == {"fa_action": 2}

    with SessionLocal() as session:
        snapshots = {
            row.client_id: row
            for row in session.scalars(select(RiskSnapshot).where(RiskSnapshot.run_id == run_id))
        }
        features = {
            row.client_id: row
            for row in session.scalars(
                select(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(BAND_CLIENT_IDS))
            )
        }

    dormant, recent = (snapshots[client_id] for client_id in BAND_CLIENT_IDS)
    assert dormant.risk_band != recent.risk_band
    assert {dormant.route, recent.route} == {"fa_action"}
    assert {dormant.queue_rank, recent.queue_rank} == {None}
    assert {features[client_id].route for client_id in BAND_CLIENT_IDS} == {"fa_action"}


def test_the_agent_starts_after_a_clean_run_when_the_setting_is_on(
    monkeypatch, db, cleanup_risk_runs
) -> None:
    settings = get_settings().model_copy(update={"agent_run_after_risk_detection": True})
    monkeypatch.setattr(risk_detection, "get_settings", lambda: settings)
    calls = []
    monkeypatch.setattr(
        risk_detection,
        "run_nightly_agent",
        lambda session, **kwargs: calls.append(kwargs),
    )

    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)
    result = _worker().run(run_id=run_id)

    with SessionLocal() as session:
        reference_date = session.get(RiskRun, run_id).reference_ts.date()

    assert result.state == "completed"
    assert len(calls) == 1
    assert calls[0]["trigger"] == "nightly"
    assert calls[0]["as_of"] == reference_date


def test_the_risk_run_still_completes_when_the_agent_fails(
    monkeypatch, db, cleanup_risk_runs
) -> None:
    settings = get_settings().model_copy(update={"agent_run_after_risk_detection": True})
    monkeypatch.setattr(risk_detection, "get_settings", lambda: settings)

    def _raise(session, **kwargs):
        raise RuntimeError("the agent model is unreachable")

    monkeypatch.setattr(risk_detection, "run_nightly_agent", _raise)

    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)
    result = _worker().run(run_id=run_id)

    assert result.state == "completed"


def test_the_agent_is_left_alone_when_the_setting_is_off(
    monkeypatch, db, cleanup_risk_runs
) -> None:
    calls = []
    monkeypatch.setattr(
        risk_detection,
        "run_nightly_agent",
        lambda session, **kwargs: calls.append(kwargs),
    )

    run_id = uuid4().hex
    cleanup_risk_runs.append(run_id)
    _worker().run(run_id=run_id)

    assert calls == []
