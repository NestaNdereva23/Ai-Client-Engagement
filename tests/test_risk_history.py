"""Tests for risk_run, risk_snapshot, and the write/read/delta functions
built over them.

Needs a real database: risk_snapshot's append-only guarantee is a unique
constraint, and that's only real when a DB enforces it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.risk.history import delta_for, latest_snapshot_for, write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult

CLIENT_ID = 90201
UNIT_FUND_ID = 10

SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


def _score(risk_score: int) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band="Watch",
        risk_reasons="No deposit in 12 months",
        fund_at_risk=float(risk_score) * 1000,
        signals=SIGNALS,
        recency_band="1-2y",
        balance_tier="Small",
        value_tier="Medium",
    )


_ROUTE = RouteResult(route="fa_watchlist", queue_rank=None, complaint_caveat=False)


@pytest.fixture
def cleanup():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(run_ids)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        session.commit()


def _run(cleanup, session) -> str:
    run_id = uuid4().hex
    cleanup.append(run_id)
    session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
    session.flush()
    return run_id


def test_a_snapshot_round_trips(db, cleanup) -> None:
    with SessionLocal() as session:
        run_id = _run(cleanup, session)
        write_snapshot(
            session,
            run_id,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(28),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.2,
        )
        session.commit()

    with SessionLocal() as session:
        row = latest_snapshot_for(session, CLIENT_ID, UNIT_FUND_ID)
    assert row is not None
    assert row.run_id == run_id
    assert row.risk_score == 28
    assert row.sig_dormant is True
    assert row.route == "fa_watchlist"


def test_a_second_write_for_the_same_run_client_fund_is_a_hard_error(db, cleanup) -> None:
    with SessionLocal() as session:
        run_id = _run(cleanup, session)
        write_snapshot(
            session,
            run_id,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(10),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=False,
            overdue_multiple=None,
        )
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        write_snapshot(
            session,
            run_id,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(90),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=False,
            overdue_multiple=None,
        )
        session.commit()


def test_delta_is_none_on_a_clients_first_ever_run(db, cleanup) -> None:
    with SessionLocal() as session:
        run_id = _run(cleanup, session)
        write_snapshot(
            session,
            run_id,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(20),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

    with SessionLocal() as session:
        assert delta_for(session, CLIENT_ID, UNIT_FUND_ID, run_id) is None


def test_delta_is_the_signed_difference_on_a_second_run(db, cleanup) -> None:
    with SessionLocal() as session:
        run_1 = _run(cleanup, session)
        write_snapshot(
            session,
            run_1,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(20),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

    with SessionLocal() as session:
        run_2 = _run(cleanup, session)
        write_snapshot(
            session,
            run_2,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(35),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.4,
        )
        session.commit()

    with SessionLocal() as session:
        assert delta_for(session, CLIENT_ID, UNIT_FUND_ID, run_2) == 15


def test_a_declining_score_gives_a_negative_delta(db, cleanup) -> None:
    with SessionLocal() as session:
        run_1 = _run(cleanup, session)
        write_snapshot(
            session,
            run_1,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(50),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

    with SessionLocal() as session:
        run_2 = _run(cleanup, session)
        write_snapshot(
            session,
            run_2,
            CLIENT_ID,
            UNIT_FUND_ID,
            _score(30),
            _ROUTE,
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

    with SessionLocal() as session:
        assert delta_for(session, CLIENT_ID, UNIT_FUND_ID, run_2) == -20


def test_history_survives_a_third_run_untouched(db, cleanup) -> None:
    """Three consecutive runs: each earlier snapshot stays exactly as
    written, and the delta always compares today against the immediately
    prior run, not the very first one.
    """
    scores = [20, 35, 60]
    run_ids = []
    with SessionLocal() as session:
        for score in scores:
            run_id = _run(cleanup, session)
            run_ids.append(run_id)
            write_snapshot(
                session,
                run_id,
                CLIENT_ID,
                UNIT_FUND_ID,
                _score(score),
                _ROUTE,
                config_version=1,
                pattern_is_reliable=True,
                overdue_multiple=1.0,
            )
            session.commit()

    with SessionLocal() as session:
        # The first two runs' rows are untouched by the third write.
        first = session.query(RiskSnapshot).filter_by(run_id=run_ids[0]).one()
        second = session.query(RiskSnapshot).filter_by(run_id=run_ids[1]).one()
        assert first.risk_score == 20
        assert second.risk_score == 35

        assert delta_for(session, CLIENT_ID, UNIT_FUND_ID, run_ids[1]) == 15
        assert delta_for(session, CLIENT_ID, UNIT_FUND_ID, run_ids[2]) == 25

        latest = latest_snapshot_for(session, CLIENT_ID, UNIT_FUND_ID)
        assert latest is not None
        assert latest.run_id == run_ids[2]
