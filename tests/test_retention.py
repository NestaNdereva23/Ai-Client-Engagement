from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import delete, select

from app.agents.situations import record_situation_counts
from app.db.models.models import IngestionStatus, RawStaging
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSituationSnapshot,
    SignalRun,
    SituationRunCount,
)
from app.db.session import SessionLocal
from app.retention import prune_raw_staging, prune_signal_snapshots
from app.services.agent_studio import compare_situation_snapshots

FUND_ID = 9871
CLIENT_ID = 987101

PULLS = {
    "ret-a1": ("retention-feed-a", "completed", 1),
    "ret-af": ("retention-feed-a", "failed", 2),
    "ret-a2": ("retention-feed-a", "completed", 3),
    "ret-a3": ("retention-feed-a", "completed", 4),
    "ret-an": ("retention-feed-a", "failed", 5),
    "ret-ar": ("retention-feed-a", "running", 6),
    "ret-b1": ("retention-feed-b", "completed", 1),
}

SIGNAL_RUNS = {
    "ret-sig-1": datetime(2098, 1, 1, 6, 0),
    "ret-sig-2": datetime(2098, 1, 2, 6, 0),
    "ret-sig-3": datetime(2098, 1, 3, 6, 0),
}


def _purge_pulls(session) -> None:
    session.execute(delete(RawStaging).where(RawStaging.run_id.in_(PULLS)))
    session.execute(delete(IngestionStatus).where(IngestionStatus.run_id.in_(PULLS)))
    session.commit()


def _purge_signal_runs(session) -> None:
    for model in (ClientSignalSnapshot, ClientSituationSnapshot, SituationRunCount):
        session.execute(delete(model).where(model.run_id.in_(SIGNAL_RUNS)))
    session.execute(delete(SignalRun).where(SignalRun.run_id.in_(SIGNAL_RUNS)))
    session.commit()


@pytest.fixture
def pulls(db: None):
    with SessionLocal() as session:
        _purge_pulls(session)
        for run_id, (endpoint, state, day) in PULLS.items():
            session.add(
                IngestionStatus(
                    run_id=run_id,
                    endpoint=endpoint,
                    state=state,
                    started_at=datetime(2099, 1, day, 6, 0),
                )
            )
            session.add(RawStaging(run_id=run_id, endpoint=endpoint, natural_key="1", payload={}))
        session.commit()

    yield

    with SessionLocal() as session:
        _purge_pulls(session)


@pytest.fixture
def signal_runs(db: None):
    with SessionLocal() as session:
        _purge_signal_runs(session)
        for run_id, started_at in SIGNAL_RUNS.items():
            session.add(SignalRun(run_id=run_id, state="completed", started_at=started_at))
        session.flush()
        for number, run_id in enumerate(SIGNAL_RUNS, start=1):
            session.add(
                ClientSignalSnapshot(
                    run_id=run_id,
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    signal_code="single_deposit",
                    is_active=True,
                )
            )
            session.add(
                ClientSituationSnapshot(
                    run_id=run_id,
                    client_id=CLIENT_ID,
                    unit_fund_id=FUND_ID,
                    situation_code="follow_up_overdue",
                    is_active=True,
                    signal_codes=[],
                )
            )
            session.add(
                SituationRunCount(
                    run_id=run_id, situation_code="follow_up_overdue", active_count=number
                )
            )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge_signal_runs(session)


def test_raw_staging_keeps_newest_two_completed_pulls_of_each_feed(pulls: None) -> None:
    with SessionLocal() as session:
        prune_raw_staging(session, keep=2)
        session.commit()

        remaining = set(
            session.scalars(select(RawStaging.run_id).where(RawStaging.run_id.in_(PULLS)))
        )

    assert remaining == {"ret-a2", "ret-a3", "ret-an", "ret-ar", "ret-b1"}


def test_snapshot_cleanup_keeps_two_runs_and_studio_compare_still_works(
    signal_runs: None,
) -> None:
    with SessionLocal() as session:
        prune_signal_snapshots(session, keep=2)
        session.commit()

        left = {
            model: set(session.scalars(select(model.run_id).where(model.run_id.in_(SIGNAL_RUNS))))
            for model in (ClientSignalSnapshot, ClientSituationSnapshot)
        }
        kept_runs = set(
            session.scalars(select(SignalRun.run_id).where(SignalRun.run_id.in_(SIGNAL_RUNS)))
        )
        comparison = compare_situation_snapshots(session, date(2098, 1, 1), date(2098, 1, 3))

    for run_ids in left.values():
        assert run_ids == {"ret-sig-2", "ret-sig-3"}
    assert kept_runs == set(SIGNAL_RUNS)
    row = next(r for r in comparison.rows if r.situation == "waiting_on_a_call")
    assert (row.count_a, row.count_b) == (1, 3)


def test_recording_counts_keeps_only_active_rows_and_can_be_repeated(db: None) -> None:
    run_id = "ret-sig-1"
    with SessionLocal() as session:
        _purge_signal_runs(session)
        session.add(SignalRun(run_id=run_id, state="completed", started_at=SIGNAL_RUNS[run_id]))
        session.flush()
        for offset, active in enumerate((True, True, False)):
            session.add(
                ClientSituationSnapshot(
                    run_id=run_id,
                    client_id=CLIENT_ID + offset,
                    unit_fund_id=FUND_ID,
                    situation_code="follow_up_overdue",
                    is_active=active,
                    signal_codes=[],
                )
            )
        session.commit()

        try:
            record_situation_counts(session, run_id)
            record_situation_counts(session, run_id)
            counts = session.execute(
                select(SituationRunCount.situation_code, SituationRunCount.active_count).where(
                    SituationRunCount.run_id == run_id
                )
            ).all()
        finally:
            _purge_signal_runs(session)

    assert counts == [("follow_up_overdue", 2)]
