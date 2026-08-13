"""Tests for GET /digest/{fa_or_fund_key}: today's persisted lines, the
empty-list-vs-404 distinction, and the overflow count.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.active_clients import ActiveClientFund
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
    "sig_drawdown": False,
    "sig_dormant": True,
    "sig_cadence_break": False,
    "sig_shrinking": False,
    "sig_fee_erosion": False,
    "sig_never_repeated": False,
}


def _score(risk_score: int, aum_at_risk: float) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band="Watch",
        risk_reasons="No contribution in 12m",
        aum_at_risk=aum_at_risk,
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
        RouteResult(route="fa_digest_watch", queue_rank=None, complaint_caveat=False),
        config_version=1,
        credible_rhythm=True,
        lapse_ratio=1.0,
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
            risk_reasons="No contribution in 12m",
            aum_at_risk=12_000.0,
            score_delta=None,
            route="fa_digest_watch",
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


def _score_with_signals(risk_score: int, aum_at_risk: float, signals: dict) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band="Watch",
        risk_reasons="No contribution in 12m",
        aum_at_risk=aum_at_risk,
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
        "sig_drawdown": True,
        "sig_dormant": True,
        "sig_cadence_break": False,
        "sig_shrinking": False,
        "sig_fee_erosion": False,
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
            RouteResult(route="fa_digest_watch", queue_rank=None, complaint_caveat=False),
            config_version=1,
            credible_rhythm=True,
            lapse_ratio=1.0,
        )
        session.commit()
        build_and_persist_digest(
            session, run_id, fa_assignment_source=StubFaAssignmentSource(), cap_per_group=12
        )
        session.commit()

    response = client.get(f"/api/v1/digest/fund:{FUND_ID}")
    assert response.status_code == 200
    line = response.json()["lines"][0]
    # SIGNAL_ORDER is cadence_break, dormant, drawdown, shrinking,
    # fee_erosion, never_repeated -- dormant and drawdown fired, in that order.
    assert line["risk_reason_tags"] == ["dormant", "drawdown"]


def test_total_aum_at_risk_includes_clients_left_off_by_the_cap(db, cleanup) -> None:
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
            RouteResult(route="fa_digest_watch", queue_rank=None, complaint_caveat=False),
            config_version=1,
            credible_rhythm=True,
            lapse_ratio=1.0,
        )
        write_snapshot(
            session,
            run_id,
            overflow,
            FUND_ID,
            _score_with_signals(40, 5_000.0, SIGNALS),
            RouteResult(route="fa_digest_watch", queue_rank=None, complaint_caveat=False),
            config_version=1,
            credible_rhythm=True,
            lapse_ratio=1.0,
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
    assert body["total_aum_at_risk"] == 25_000.0


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
                RouteResult(route="fa_digest_watch", queue_rank=None, complaint_caveat=False),
                config_version=1,
                credible_rhythm=True,
                lapse_ratio=1.0,
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
                risk_reasons="No contribution in 12m",
                aum_at_risk=10_000.0,
                config_version=1,
                route="fa_digest_watch",
                queue_rank=None,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=has_data,
                unit_fund_id=FUND_ID,
                client_code="C93206",
                balance=10_000.0,
                n_purchases=1,
                n_sales=0,
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
