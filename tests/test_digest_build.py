"""Tests for digest/build.py: grouping (with the FA-to-fund fallback), sort
order, the per-group cap, the complaint caveat, and that a rebuild from the
same risk_run_id gives back the same lines.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models.complaints import ClientComplaint
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.digest.build import build_digest
from app.ingestion.fa_assignment_source import FaAssignmentRecord
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult

FUND_ID = 930

SIGNALS = {
    "sig_drawdown": False,
    "sig_dormant": True,
    "sig_cadence_break": False,
    "sig_shrinking": False,
    "sig_fee_erosion": False,
    "sig_never_repeated": False,
}


class FakeFaAssignmentSource:
    """Returns whatever mapping the test hands it, keyed by (client_id, unit_fund_id)."""

    def __init__(self, mapping: dict[tuple[int, int], int | None]) -> None:
        self._mapping = mapping

    def fetch_assignments(self, client_ids):
        return [
            FaAssignmentRecord(client_id=cid, unit_fund_id=ufid, fa_id=fa_id, source="fake")
            for (cid, ufid), fa_id in self._mapping.items()
            if cid in client_ids
        ]


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
    client_ids: list[int] = []
    yield run_ids, client_ids
    with SessionLocal() as session:
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(run_ids)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        if client_ids:
            session.execute(
                delete(ClientComplaint).where(ClientComplaint.client_id.in_(client_ids))
            )
        session.commit()


def _run(session) -> str:
    run_id = uuid4().hex
    session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
    session.flush()
    return run_id


def _seed(
    session,
    run_id: str,
    client_id: int,
    risk_score: int,
    aum_at_risk: float,
    route: str,
    queue_rank: int | None = None,
) -> None:
    write_snapshot(
        session,
        run_id,
        client_id,
        FUND_ID,
        _score(risk_score, aum_at_risk),
        RouteResult(route=route, queue_rank=queue_rank, complaint_caveat=False),
        config_version=1,
        credible_rhythm=True,
        lapse_ratio=1.0,
    )


def test_falls_back_to_fund_when_fa_id_is_null(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    client_a, client_b = 93001, 93002
    client_ids.extend([client_a, client_b])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, client_a, 40, 10_000.0, "fa_digest_watch")
        _seed(session, run_id, client_b, 40, 10_000.0, "fa_digest_watch")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    assert set(result.groups) == {f"fund:{FUND_ID}"}
    assert {line.client_id for line in result.groups[f"fund:{FUND_ID}"].lines} == {
        client_a,
        client_b,
    }


def test_groups_by_fa_id_when_one_is_assigned(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    client_a, client_b = 93003, 93004
    client_ids.extend([client_a, client_b])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, client_a, 40, 10_000.0, "fa_digest_watch")
        _seed(session, run_id, client_b, 40, 10_000.0, "fa_digest_watch")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({(client_a, FUND_ID): 7}),
            cap_per_group=12,
        )

    assert set(result.groups) == {"fa:7", f"fund:{FUND_ID}"}
    assert [line.client_id for line in result.groups["fa:7"].lines] == [client_a]
    assert [line.client_id for line in result.groups[f"fund:{FUND_ID}"].lines] == [client_b]


def test_sort_order_is_aum_at_risk_not_score(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    high_score_low_aum, low_score_high_aum = 93005, 93006
    client_ids.extend([high_score_low_aum, low_score_high_aum])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, high_score_low_aum, 80, 1_000.0, "fa_digest_watch")
        _seed(session, run_id, low_score_high_aum, 30, 5_000.0, "fa_digest_watch")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    lines = result.groups[f"fund:{FUND_ID}"].lines
    assert [line.client_id for line in lines] == [low_score_high_aum, high_score_low_aum]
    assert [line.rank for line in lines] == [1, 2]


def test_cap_leaves_the_rest_countable_as_overflow(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    ids = [93007, 93008, 93009]
    client_ids.extend(ids)

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        for i, client_id in enumerate(ids):
            _seed(session, run_id, client_id, 40, float(1000 * (i + 1)), "fa_digest_watch")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=2,
        )

    group = result.groups[f"fund:{FUND_ID}"]
    assert group.total_eligible == 3
    assert len(group.lines) == 2
    assert group.total_eligible - len(group.lines) == 1  # "and 1 more"


def test_open_complaint_sets_the_caveat(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    with_complaint, without = 93010, 93011
    client_ids.extend([with_complaint, without])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, with_complaint, 40, 10_000.0, "fa_digest_watch")
        _seed(session, run_id, without, 40, 9_000.0, "fa_digest_watch")
        session.add(
            ClientComplaint(
                client_id=with_complaint,
                opened_at="2026-08-01",
                status="open",
                category="service",
                channel="call",
            )
        )
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    lines = {line.client_id: line for line in result.groups[f"fund:{FUND_ID}"].lines}
    assert lines[with_complaint].complaint_caveat is True
    assert lines[without].complaint_caveat is False


def test_rebuilding_from_the_same_run_gives_the_same_lines(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    client_id = 93012
    client_ids.append(client_id)

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, client_id, 55, 20_000.0, "fa_call_priority", queue_rank=1)
        session.commit()

        first = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )
        second = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    assert first.groups.keys() == second.groups.keys()
    for key in first.groups:
        assert first.groups[key].lines == second.groups[key].lines
        assert first.groups[key].total_eligible == second.groups[key].total_eligible
