"""Tests for workers/digest.py: persisting a built digest and auditing its
generation, and that persisting it twice from the same risk_run_id gives
back the same line content.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models.audit import AuditLog
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult
from app.workers.digest import build_and_persist_digest

FUND_ID = 931

SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


class FakeFaAssignmentSource:
    def fetch_assignments(self, client_ids):
        return []


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
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_(run_ids)))
        session.commit()


def _run(session) -> str:
    run_id = uuid4().hex
    session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
    session.flush()
    return run_id


def test_persists_lines_and_audits_generation(db, cleanup) -> None:
    client_id = 93101

    with SessionLocal() as session:
        run_id = _run(session)
        cleanup.append(run_id)
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
        session.commit()

        digest_run = build_and_persist_digest(
            session, run_id, fa_assignment_source=FakeFaAssignmentSource(), cap_per_group=12
        )
        session.commit()

    with SessionLocal() as session:
        stored_run = session.get(DigestRun, digest_run.digest_run_id)
        assert stored_run is not None
        assert stored_run.risk_run_id == run_id

        lines = session.scalars(
            select(DigestLine).where(DigestLine.digest_run_id == digest_run.digest_run_id)
        ).all()
        assert len(lines) == 1
        assert lines[0].client_id == client_id
        assert lines[0].group_key == f"fund:{FUND_ID}"

        audit = session.scalar(
            select(AuditLog).where(AuditLog.run_id == run_id, AuditLog.action == "generate")
        )
        assert audit is not None
        assert audit.entity_type == "digest_run"
        assert audit.detail["lines"] == 1


def test_persisting_twice_from_the_same_run_gives_the_same_line_content(db, cleanup) -> None:
    client_id = 93102

    with SessionLocal() as session:
        run_id = _run(session)
        cleanup.append(run_id)
        write_snapshot(
            session,
            run_id,
            client_id,
            FUND_ID,
            _score(60, 30_000.0),
            RouteResult(route="fa_call_priority", queue_rank=1, complaint_caveat=False),
            config_version=1,
            pattern_is_reliable=True,
            overdue_multiple=1.0,
        )
        session.commit()

        first = build_and_persist_digest(
            session, run_id, fa_assignment_source=FakeFaAssignmentSource(), cap_per_group=12
        )
        second = build_and_persist_digest(
            session, run_id, fa_assignment_source=FakeFaAssignmentSource(), cap_per_group=12
        )
        session.commit()

    def _line_content(digest_run_id: int) -> list[tuple]:
        with SessionLocal() as session:
            rows = session.scalars(
                select(DigestLine)
                .where(DigestLine.digest_run_id == digest_run_id)
                .order_by(DigestLine.rank)
            ).all()
            return [
                (
                    r.group_key,
                    r.rank,
                    r.client_id,
                    r.unit_fund_id,
                    r.risk_score,
                    r.risk_band,
                    r.fund_at_risk,
                    r.route,
                    r.in_call_queue,
                )
                for r in rows
            ]

    assert _line_content(first.digest_run_id) == _line_content(second.digest_run_id)
