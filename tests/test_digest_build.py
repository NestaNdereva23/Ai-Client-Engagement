"""Tests for digest/build.py: grouping (an advisor's own queue, and the
fund wide group beside it), sort order, the per-group cap's batch
boundaries, the complaint caveat, and that a rebuild from the same
risk_run_id gives back the same lines.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientInteraction
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
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
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
    client_ids: list[int] = []
    yield run_ids, client_ids
    with SessionLocal() as session:
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(run_ids)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        if client_ids:
            session.execute(
                delete(ClientComplaint).where(ClientComplaint.client_id.in_(client_ids))
            )
            session.execute(
                delete(ActiveClientInteraction).where(
                    ActiveClientInteraction.client_id.in_(client_ids)
                )
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
    fund_at_risk: float,
    route: str,
    queue_rank: int | None = None,
) -> None:
    write_snapshot(
        session,
        run_id,
        client_id,
        FUND_ID,
        _score(risk_score, fund_at_risk),
        RouteResult(route=route, queue_rank=queue_rank, complaint_caveat=False),
        config_version=1,
        pattern_is_reliable=True,
        overdue_multiple=1.0,
    )


def test_unassigned_clients_land_in_the_fund_group(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    client_a, client_b = 93001, 93002
    client_ids.extend([client_a, client_b])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, client_a, 40, 10_000.0, "fa_watchlist")
        _seed(session, run_id, client_b, 40, 10_000.0, "fa_watchlist")
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
        _seed(session, run_id, client_a, 40, 10_000.0, "fa_watchlist")
        _seed(session, run_id, client_b, 40, 10_000.0, "fa_watchlist")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({(client_a, FUND_ID): "fa-7"}),
            cap_per_group=12,
        )

    assert set(result.groups) == {"fa:fa-7", f"fund:{FUND_ID}"}
    assert [line.client_id for line in result.groups["fa:fa-7"].lines] == [client_a]
    # The fund group is the whole fund, so it holds the assigned client too,
    # not only the one nobody owns.
    fund_group = result.groups[f"fund:{FUND_ID}"]
    assert {line.client_id for line in fund_group.lines} == {client_a, client_b}
    assert fund_group.total_eligible == 2


def test_sort_order_is_fund_at_risk_not_score(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    high_score_low_fund_at_risk, low_score_high_fund_at_risk = 93005, 93006
    client_ids.extend([high_score_low_fund_at_risk, low_score_high_fund_at_risk])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, high_score_low_fund_at_risk, 80, 1_000.0, "fa_watchlist")
        _seed(session, run_id, low_score_high_fund_at_risk, 30, 5_000.0, "fa_watchlist")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    lines = result.groups[f"fund:{FUND_ID}"].lines
    assert [line.client_id for line in lines] == [
        low_score_high_fund_at_risk,
        high_score_low_fund_at_risk,
    ]
    assert [line.rank for line in lines] == [1, 2]


def test_cap_size_sets_batch_boundaries_but_keeps_everyone(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    ids = [93007, 93008, 93009]
    client_ids.extend(ids)

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        for i, client_id in enumerate(ids):
            _seed(session, run_id, client_id, 40, float(1000 * (i + 1)), "fa_watchlist")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=2,
        )

    group = result.groups[f"fund:{FUND_ID}"]
    # cap_per_group=2 with 3 eligible rows makes two batches (2, then 1),
    # but build_digest never drops anyone -- reveal-the-next-batch is a
    # read-time decision (app/services/digest.py), not a build-time one.
    assert group.total_eligible == 3
    assert len(group.lines) == 3
    assert [line.batch for line in group.lines] == [0, 0, 1]


def test_open_complaint_sets_the_caveat(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    with_complaint, without = 93010, 93011
    client_ids.extend([with_complaint, without])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, with_complaint, 40, 10_000.0, "fa_watchlist")
        _seed(session, run_id, without, 40, 9_000.0, "fa_watchlist")
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


def _log_interaction(
    session, client_id: int, *, type: str = "call_logged", risk_band_at_interaction: str | None
) -> None:
    session.add(
        ActiveClientInteraction(
            client_id=client_id,
            unit_fund_id=FUND_ID,
            type=type,
            reviewer_id="fa-1",
            risk_band_at_interaction=risk_band_at_interaction,
        )
    )


def test_untouched_client_outranks_a_touched_one_despite_lower_fund_at_risk(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    untouched, touched = 93013, 93014
    client_ids.extend([untouched, touched])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, untouched, 40, 5_000.0, "fa_watchlist")
        _seed(session, run_id, touched, 40, 50_000.0, "fa_watchlist")
        # Same band now ("Watch", _score's hardcoded band) as when the call
        # was logged -- nothing got worse, so this stays deprioritized.
        _log_interaction(session, touched, risk_band_at_interaction="Watch")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    lines = {line.client_id: line for line in result.groups[f"fund:{FUND_ID}"].lines}
    ordered = [line.client_id for line in result.groups[f"fund:{FUND_ID}"].lines]
    assert ordered == [untouched, touched]
    assert lines[untouched].deprioritized is False
    assert lines[touched].deprioritized is True


def test_escalated_band_since_interaction_ranks_with_the_untouched_tier(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    untouched, escalated, deprioritized = 93015, 93016, 93017
    client_ids.extend([untouched, escalated, deprioritized])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, untouched, 40, 10_000.0, "fa_watchlist")
        _seed(session, run_id, escalated, 40, 5_000.0, "fa_watchlist")
        _seed(session, run_id, deprioritized, 40, 50_000.0, "fa_watchlist")
        # escalated's band was "Low" when dismissed; today's run scores them
        # "Watch" (_score's hardcoded band) -- risen, so the escape hatch
        # applies even though their fund_at_risk is the lowest of the three.
        _log_interaction(session, escalated, type="dismissed", risk_band_at_interaction="Low")
        _log_interaction(session, deprioritized, risk_band_at_interaction="Watch")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    lines = {line.client_id: line for line in result.groups[f"fund:{FUND_ID}"].lines}
    ordered = [line.client_id for line in result.groups[f"fund:{FUND_ID}"].lines]
    # Both tiers still rank by fund_at_risk within themselves: untouched
    # (10,000) beats escalated (5,000) in tier 0, then deprioritized
    # (50,000) trails last despite the largest fund_at_risk of the three.
    assert ordered == [untouched, escalated, deprioritized]
    assert lines[untouched].deprioritized is False
    assert lines[escalated].deprioritized is False
    assert lines[deprioritized].deprioritized is True


def test_interaction_with_no_band_on_file_stays_deprioritized(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    client_id = 93018
    client_ids.append(client_id)

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, client_id, 40, 10_000.0, "fa_watchlist")
        # Logged before the client was ever scored -- nothing to compare
        # today's band against, so it can't earn the escalation escape hatch.
        _log_interaction(session, client_id, risk_band_at_interaction=None)
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({}),
            cap_per_group=12,
        )

    line = result.groups[f"fund:{FUND_ID}"].lines[0]
    assert line.deprioritized is True


def test_a_lent_client_groups_under_the_stand_in(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    owned, lent = 93041, 93042
    client_ids.extend([owned, lent])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, owned, 40, 10_000.0, "fa_call_priority", queue_rank=1)
        _seed(session, run_id, lent, 40, 9_000.0, "fa_call_priority", queue_rank=2)
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource(
                {(owned, FUND_ID): "fa-7", (lent, FUND_ID): "fa-7"}
            ),
            cap_per_group=12,
            covering={lent: "fa-8"},
        )

    assert [line.client_id for line in result.groups["fa:fa-7"].lines] == [owned]
    lent_line = result.groups["fa:fa-8"].lines[0]
    assert lent_line.client_id == lent
    assert lent_line.covering_for_fa_id == "fa-7"
    assert result.groups["fa:fa-7"].lines[0].covering_for_fa_id is None


def test_fund_group_is_the_union_of_every_advisor_book(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    of_fa_7, of_fa_8, unowned = 93043, 93044, 93045
    client_ids.extend([of_fa_7, of_fa_8, unowned])

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, of_fa_7, 40, 30_000.0, "fa_watchlist")
        _seed(session, run_id, of_fa_8, 40, 20_000.0, "fa_watchlist")
        _seed(session, run_id, unowned, 40, 10_000.0, "fa_watchlist")
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource(
                {(of_fa_7, FUND_ID): "fa-7", (of_fa_8, FUND_ID): "fa-8"}
            ),
            cap_per_group=12,
        )

    fund_group = result.groups[f"fund:{FUND_ID}"]
    assert [line.client_id for line in fund_group.lines] == [of_fa_7, of_fa_8, unowned]
    assert fund_group.total_eligible == 3
    assert fund_group.total_fund_at_risk == pytest.approx(60_000.0)
    assert [line.rank for line in fund_group.lines] == [1, 2, 3]

    # Each advisor still sees only their own, and every one of them also
    # shows up in the fund group.
    assert [line.client_id for line in result.groups["fa:fa-7"].lines] == [of_fa_7]
    assert [line.client_id for line in result.groups["fa:fa-8"].lines] == [of_fa_8]


def test_a_fund_line_never_records_who_is_covering(db, cleanup) -> None:
    run_ids, client_ids = cleanup
    lent = 93046
    client_ids.append(lent)

    with SessionLocal() as session:
        run_id = _run(session)
        run_ids.append(run_id)
        _seed(session, run_id, lent, 40, 9_000.0, "fa_call_priority", queue_rank=1)
        session.commit()

        result = build_digest(
            session,
            run_id,
            fa_assignment_source=FakeFaAssignmentSource({(lent, FUND_ID): "fa-7"}),
            cap_per_group=12,
            covering={lent: "fa-8"},
        )

    # The stand-in's queue says who they are calling for. The fund view is
    # not a queue, so it says nothing about it.
    assert result.groups["fa:fa-8"].lines[0].covering_for_fa_id == "fa-7"
    assert result.groups[f"fund:{FUND_ID}"].lines[0].covering_for_fa_id is None
