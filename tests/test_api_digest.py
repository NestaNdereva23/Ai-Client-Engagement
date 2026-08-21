"""Tests for GET /digest/{fa_or_fund_key}: today's unlocked lines, the
empty-list-vs-404 distinction, the overflow count, and the batch unlock.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.ingestion.fa_assignment_source import StubFaAssignmentSource
from app.main import app
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult
from app.workers.digest import build_and_persist_digest

client = TestClient(app)

FUND_ID = 932

SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


def _score(risk_score: int, fund_at_risk: float) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band="Watch",
        risk_reasons="No deposit in 12 months",
        fund_at_risk=fund_at_risk,
        signals=SIGNALS,
        recency_band="1-2y",
        balance_tier="Small",
        value_tier="Medium",
    )


@pytest.fixture
def cleanup():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        digest_run_ids = session.scalars(
            select(DigestRun.digest_run_id).where(DigestRun.risk_run_id.in_(run_ids))
        ).all()
        if digest_run_ids:
            session.execute(delete(DigestLine).where(DigestLine.digest_run_id.in_(digest_run_ids)))
            session.execute(delete(DigestRun).where(DigestRun.digest_run_id.in_(digest_run_ids)))
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(run_ids)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        session.commit()


def _seed_digest(session, run_id: str, client_id: int, cap: int = 12) -> None:
    write_snapshot(
        session,
        run_id,
        client_id,
        FUND_ID,
        _score(45, 12_000.0),
        RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
        config_version=1,
        pattern_is_reliable=True,
        overdue_multiple=1.0,
    )
    session.flush()
    digest_run = DigestRun(risk_run_id=run_id)
    session.add(digest_run)
    session.flush()
    session.add(
        DigestLine(
            digest_run_id=digest_run.digest_run_id,
            group_key=f"fund:{FUND_ID}",
            group_total=1,
            rank=1,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            risk_score=45,
            risk_band="Watch",
            risk_reasons="No deposit in 12 months",
            fund_at_risk=12_000.0,
            score_delta=None,
            route="fa_watchlist",
            in_call_queue=False,
            complaint_caveat=False,
        )
    )


def test_returns_todays_lines_for_the_group(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    client_id = 93201

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        _seed_digest(session, run_id, client_id)
        session.commit()

    response = client.get(f"/api/v1/digest/fund:{FUND_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["group_key"] == f"fund:{FUND_ID}"
    assert body["total_eligible"] == 1
    assert body["overflow_count"] == 0
    assert len(body["lines"]) == 1
    assert body["lines"][0]["client_id"] == client_id


def test_a_group_with_no_lines_today_is_an_empty_list_not_a_404(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    client_id = 93202

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        _seed_digest(session, run_id, client_id)
        session.commit()

    response = client.get("/api/v1/digest/fund:999999")
    assert response.status_code == 200
    body = response.json()
    assert body["lines"] == []
    assert body["total_eligible"] == 0


def test_no_digest_generated_today_is_a_404(db) -> None:
    response = client.get("/api/v1/digest/fund:1")
    assert response.status_code == 404


def _score_with_signals(risk_score: int, fund_at_risk: float, signals: dict) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band="Watch",
        risk_reasons="No deposit in 12 months",
        fund_at_risk=fund_at_risk,
        signals=signals,
        recency_band="1-2y",
        balance_tier="Small",
        value_tier="Medium",
    )


def test_risk_reason_tags_match_the_fired_signals(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    client_id = 93203
    signals = {
        "sig_heavy_withdrawal": True,
        "sig_dormant": True,
        "sig_broken_pattern": False,
        "sig_shrinking": False,
        "sig_going_dormant": False,
        "sig_never_repeated": False,
    }

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        write_snapshot(
            session,
            run_id,
            client_id,
            FUND_ID,
            _score_with_signals(45, 12_000.0, signals),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()
        build_and_persist_digest(
            session, run_id, fa_assignment_source=StubFaAssignmentSource(), cap_per_group=12
        )
        session.commit()

    response = client.get(f"/api/v1/digest/fund:{FUND_ID}")
    assert response.status_code == 200
    line = response.json()["lines"][0]
    # SIGNAL_ORDER is broken_pattern, dormant, heavy_withdrawal, shrinking,
    # going_dormant, never_repeated -- dormant and heavy_withdrawal fired,
    # in that order.
    assert line["risk_reason_tags"] == ["dormant", "heavy_withdrawal"]


def test_total_fund_at_risk_includes_clients_left_off_by_the_cap(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    shown, overflow = 93204, 93205

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        write_snapshot(
            session,
            run_id,
            shown,
            FUND_ID,
            _score_with_signals(50, 20_000.0, SIGNALS),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        write_snapshot(
            session,
            run_id,
            overflow,
            FUND_ID,
            _score_with_signals(40, 5_000.0, SIGNALS),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()
        build_and_persist_digest(
            session, run_id, fa_assignment_source=StubFaAssignmentSource(), cap_per_group=1
        )
        session.commit()

    response = client.get(f"/api/v1/digest/fund:{FUND_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["overflow_count"] == 1
    assert len(body["lines"]) == 1
    assert body["total_fund_at_risk"] == 25_000.0


def test_next_batch_unlocks_once_the_current_batch_is_touched(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    first_client, second_client = 93208, 93209

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        # Higher fund_at_risk ranks first, so first_client lands in batch 0
        # and second_client in batch 1 once cap_per_group=1 splits them.
        write_snapshot(
            session,
            run_id,
            first_client,
            FUND_ID,
            _score_with_signals(45, 20_000.0, SIGNALS),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        write_snapshot(
            session,
            run_id,
            second_client,
            FUND_ID,
            _score_with_signals(45, 5_000.0, SIGNALS),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()
        build_and_persist_digest(
            session, run_id, fa_assignment_source=StubFaAssignmentSource(), cap_per_group=1
        )
        session.commit()

    try:
        before = client.get(f"/api/v1/digest/fund:{FUND_ID}").json()
        assert [line["client_id"] for line in before["lines"]] == [first_client]
        assert before["overflow_count"] == 1

        with SessionLocal() as session:
            session.add(
                ActiveClientInteraction(
                    client_id=first_client,
                    unit_fund_id=FUND_ID,
                    type="call_logged",
                    reviewer_id="fa-1",
                    # Same band ("Watch") as when this run scored them --
                    # nothing got worse, so this clears batch 0.
                    risk_band_at_interaction="Watch",
                )
            )
            session.commit()

        after = client.get(f"/api/v1/digest/fund:{FUND_ID}").json()
        assert {line["client_id"] for line in after["lines"]} == {first_client, second_client}
        assert after["overflow_count"] == 0
        by_client = {line["client_id"]: line for line in after["lines"]}
        assert by_client[first_client]["deprioritized"] is True
        assert by_client[second_client]["deprioritized"] is False
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ActiveClientInteraction).where(
                    ActiveClientInteraction.client_id.in_([first_client, second_client])
                )
            )
            session.commit()


def test_an_escalated_touch_keeps_the_next_batch_locked(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    first_client, second_client = 93210, 93211

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        write_snapshot(
            session,
            run_id,
            first_client,
            FUND_ID,
            _score_with_signals(45, 20_000.0, SIGNALS),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        write_snapshot(
            session,
            run_id,
            second_client,
            FUND_ID,
            _score_with_signals(45, 5_000.0, SIGNALS),
            RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()
        build_and_persist_digest(
            session, run_id, fa_assignment_source=StubFaAssignmentSource(), cap_per_group=1
        )
        session.commit()

    try:
        with SessionLocal() as session:
            session.add(
                ActiveClientInteraction(
                    client_id=first_client,
                    unit_fund_id=FUND_ID,
                    type="dismissed",
                    reviewer_id="fa-1",
                    # Was "Low" when dismissed; this run scores "Watch" --
                    # risen, so first_client stays on the active tier and
                    # batch 0 never clears.
                    risk_band_at_interaction="Low",
                )
            )
            session.commit()

        after = client.get(f"/api/v1/digest/fund:{FUND_ID}").json()
        assert [line["client_id"] for line in after["lines"]] == [first_client]
        assert after["overflow_count"] == 1
        assert after["lines"][0]["deprioritized"] is False
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ActiveClientInteraction).where(
                    ActiveClientInteraction.client_id.in_([first_client, second_client])
                )
            )
            session.commit()


def test_briefing_available_reflects_current_risk_and_active_data(db, cleanup) -> None:
    run_id = uuid4().hex
    cleanup.append(run_id)
    has_data, missing_data = 93206, 93207

    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        for client_id in (has_data, missing_data):
            write_snapshot(
                session,
                run_id,
                client_id,
                FUND_ID,
                _score_with_signals(45, 10_000.0, SIGNALS),
                RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False),
                config_version=1,
                pattern_is_reliable=True,
                overdue_multiple=1.0,
            )
        # Only has_data gets the client_risk_features + active_client_fund
        # rows get_briefing needs; missing_data has neither.
        session.add(
            ClientRiskFeatures(
                client_id=has_data,
                unit_fund_id=FUND_ID,
                balance_tier="Small",
                **SIGNALS,
                risk_score=45,
                risk_band="Watch",
                risk_reasons="No deposit in 12 months",
                fund_at_risk=10_000.0,
                config_version=1,
                route="fa_watchlist",
                queue_rank=None,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=has_data,
                unit_fund_id=FUND_ID,
                client_code="C93206",
                balance=10_000.0,
                n_deposits=1,
                n_withdrawals=0,
            )
        )
        session.commit()
        build_and_persist_digest(
            session, run_id, fa_assignment_source=StubFaAssignmentSource(), cap_per_group=12
        )
        session.commit()

    try:
        response = client.get(f"/api/v1/digest/fund:{FUND_ID}")
        assert response.status_code == 200
        by_client = {line["client_id"]: line for line in response.json()["lines"]}
        assert by_client[has_data]["briefing_available"] is True
        assert by_client[missing_data]["briefing_available"] is False
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id == has_data,
                    ClientRiskFeatures.unit_fund_id == FUND_ID,
                )
            )
            session.execute(
                delete(ActiveClientFund).where(
                    ActiveClientFund.client_id == has_data,
                    ActiveClientFund.unit_fund_id == FUND_ID,
                )
            )
            session.commit()
