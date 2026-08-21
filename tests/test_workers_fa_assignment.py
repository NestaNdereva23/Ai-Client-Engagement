"""Tests for workers/fa_assignment.py: writing the nightly allocation.

Covers what the pure allocation cannot: the advisor is written onto every
one of a client's rows, ownership survives a second run, and the write is
audited.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.config import FaRecord
from app.db.models.audit import AuditLog
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import RiskRun
from app.db.session import SessionLocal
from app.risk.fa_allocation import ClientLoad
from app.workers.fa_assignment import allocate_and_persist

CLIENT_A = 26151
CLIENT_B = 26152
FUND_ONE = 941
FUND_TWO = 942

ROSTER = (
    FaRecord(fa_id="fa-61", name="FA Sixty One", email="fa61@example.com", daily_capacity=1),
    FaRecord(fa_id="fa-62", name="FA Sixty Two", email="fa62@example.com", daily_capacity=1),
)

KEYS = [(CLIENT_A, FUND_ONE), (CLIENT_A, FUND_TWO), (CLIENT_B, FUND_ONE)]


@pytest.fixture
def cleanup():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        session.execute(
            delete(FaAssignment).where(FaAssignment.client_id.in_([CLIENT_A, CLIENT_B]))
        )
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_(run_ids)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        session.commit()


def _run(session, cleanup) -> str:
    run_id = uuid4().hex
    session.add(RiskRun(run_id=run_id, state="running", config_version=1))
    session.flush()
    cleanup.append(run_id)
    return run_id


def _loads() -> list[ClientLoad]:
    return [
        ClientLoad(client_id=CLIENT_A, fund_at_risk=900.0, in_call_queue=True),
        ClientLoad(client_id=CLIENT_B, fund_at_risk=100.0, in_call_queue=True),
    ]


def test_advisor_is_written_onto_every_fund_row(db, cleanup) -> None:
    with SessionLocal() as session:
        run_id = _run(session, cleanup)
        allocation = allocate_and_persist(
            session, run_id, roster=ROSTER, clients=_loads(), keys=KEYS
        )
        session.commit()

    with SessionLocal() as session:
        rows = session.scalars(select(FaAssignment).where(FaAssignment.client_id == CLIENT_A)).all()

    assert {row.unit_fund_id for row in rows} == {FUND_ONE, FUND_TWO}
    assert {row.fa_id for row in rows} == {allocation.owner[CLIENT_A]}
    assert all(row.source == "roster" and row.fa_name.startswith("FA Sixty") for row in rows)


def test_a_second_run_leaves_ownership_alone(db, cleanup) -> None:
    with SessionLocal() as session:
        first = allocate_and_persist(
            session, _run(session, cleanup), roster=ROSTER, clients=_loads(), keys=KEYS
        )
        session.commit()

    with SessionLocal() as session:
        second = allocate_and_persist(
            session, _run(session, cleanup), roster=ROSTER, clients=_loads(), keys=KEYS
        )
        session.commit()

    assert second.owner == first.owner


def test_the_write_is_audited(db, cleanup) -> None:
    with SessionLocal() as session:
        run_id = _run(session, cleanup)
        allocate_and_persist(session, run_id, roster=ROSTER, clients=_loads(), keys=KEYS)
        session.commit()

    with SessionLocal() as session:
        entry = session.scalar(
            select(AuditLog).where(
                AuditLog.run_id == run_id,
                AuditLog.entity_type == "fa_assignment",
                AuditLog.action == "allocate",
            )
        )

    assert entry is not None
    assert entry.detail["advisors"] == 2
    assert entry.detail["clients"] == 2
    assert entry.detail["rows_written"] == 3
    assert entry.detail["first_time"] == 2


def test_an_empty_roster_writes_nothing(db, cleanup) -> None:
    with SessionLocal() as session:
        run_id = _run(session, cleanup)
        result = allocate_and_persist(session, run_id, roster=(), clients=_loads(), keys=KEYS)
        session.commit()

    assert result.owner == {}
    with SessionLocal() as session:
        assert (
            session.scalars(
                select(FaAssignment).where(FaAssignment.client_id.in_([CLIENT_A, CLIENT_B]))
            ).all()
            == []
        )
