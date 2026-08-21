"""Build one morning digest from a completed risk_run's snapshot rows.

Pure over already-fetched inputs, same discipline as risk/scoring.py and
risk/routing.py -- no persistence here. app/workers/digest.py calls this and
writes the result to digest_run/digest_line; app/services/digest.py reads
what was already written back out for the API.

Within each group, a client-fund an FA manager has already acted on (call
logged, snoozed, dismissed, or emailed) ranks below every untouched one,
so a client nobody has touched yet always gets first shot at a slot
instead of re-competing on equal footing with one that's already been
worked. A touched client only ranks back up front if their risk band has
gone up since that interaction -- see is_deprioritized.

Every eligible row is built and returned here, not just the first
cap_per_group of them -- cap_per_group only decides where one batch ends
and the next begins (line.batch). Nobody is ever dropped: whoever is
short on their current batch gets handed the next one once it clears.
That reveal-the-next-batch decision is a live, read-time one, so it lives
in app/services/digest.py, not here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientInteraction
from app.db.models.complaints import ClientComplaint
from app.db.models.digest import DigestLine
from app.db.models.risk import RiskSnapshot
from app.ingestion.fa_assignment_source import FaAssignmentSource
from app.risk.history import delta_for
from app.risk.scoring import band_rank
from app.risk.signals import fired_signal_tags

# Routes a digest line ever exists for: the two an FA needs to see, in the
# order the notebook renders them (call queue first).
DIGEST_ROUTES = ("fa_call_priority", "fa_watchlist")


def group_key_for(fa_id: str | None, unit_fund_id: int) -> str:
    """ "fa:<fa_id>", or "fund:<unit_fund_id>" when there is no FA to group by."""
    return f"fa:{fa_id}" if fa_id is not None else f"fund:{unit_fund_id}"


@dataclass
class DigestLineData:
    """One client-fund's line, already ranked within its group.

    batch is rank floor-divided by the group's cap_per_group -- 0 for the
    first cap_per_group rows, 1 for the next cap_per_group, and so on.
    Every batch is built and persisted; app/services/digest.py decides how
    many of them are unlocked yet.
    """

    group_key: str
    rank: int
    batch: int
    client_id: int
    unit_fund_id: int
    risk_score: int
    risk_band: str
    risk_reasons: str
    risk_reason_tags: list[str]
    fund_at_risk: float
    score_delta: int | None
    route: str
    in_call_queue: bool
    complaint_caveat: bool
    deprioritized: bool
    # The advisor who owns this client, set only when someone else is
    # calling them tonight. None on an ordinary line.
    covering_for_fa_id: str | None = None


@dataclass
class DigestGroupData:
    group_key: str
    total_eligible: int
    total_fund_at_risk: float
    lines: list[DigestLineData]


@dataclass
class DigestBuildResult:
    risk_run_id: str
    groups: dict[str, DigestGroupData]

    @property
    def lines(self) -> list[DigestLineData]:
        return [line for group in self.groups.values() for line in group.lines]


def latest_interactions_for(
    session: Session, keys: list[tuple[int, int]]
) -> dict[tuple[int, int], ActiveClientInteraction]:
    """The most recent ActiveClientInteraction row for each (client_id,
    unit_fund_id) in keys that has ever logged one, keyed the same way.

    Two plain-aggregate reads rather than a window function, the same idiom
    services/active_clients.py::list_active_roster already uses for its own
    "last interaction" lookup: first the latest id per key (id, not
    created_at, since id is the monotonic tiebreaker two same-tick writes
    can't share), then the full rows for those ids.

    Public because app/services/digest.py calls this too, at read time, to
    tell live whether a batch has been cleared -- not just at build time.
    """
    if not keys:
        return {}
    latest_ids = {
        (row.client_id, row.unit_fund_id): row.latest_id
        for row in session.execute(
            select(
                ActiveClientInteraction.client_id,
                ActiveClientInteraction.unit_fund_id,
                func.max(ActiveClientInteraction.id).label("latest_id"),
            )
            .where(
                tuple_(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id).in_(
                    keys
                )
            )
            .group_by(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id)
        ).all()
    }
    if not latest_ids:
        return {}
    rows = session.scalars(
        select(ActiveClientInteraction).where(ActiveClientInteraction.id.in_(latest_ids.values()))
    )
    return {(row.client_id, row.unit_fund_id): row for row in rows}


def is_deprioritized(
    row: RiskSnapshot | DigestLine,
    latest_interactions: dict[tuple[int, int], ActiveClientInteraction],
) -> bool:
    """True for a client-fund an FA manager already acted on, whose risk
    band hasn't gone up since. False (never deprioritized) for a client
    nobody has touched yet, or one whose band has risen since the last
    interaction -- that rise is the escape hatch back to the front of the
    queue.

    A touched row with no risk_band_at_interaction on file (the interaction
    was logged before the client was ever scored) has nothing to compare
    against, so it stays deprioritized rather than guessing it improved.

    row is a RiskSnapshot at build time or a persisted DigestLine at read
    time (app/services/digest.py, deciding whether a batch has cleared
    live) -- both carry client_id, unit_fund_id and risk_band, which is all
    this needs.
    """
    interaction = latest_interactions.get((row.client_id, row.unit_fund_id))
    if interaction is None:
        return False
    if interaction.risk_band_at_interaction is None:
        return True
    return band_rank(row.risk_band) <= band_rank(interaction.risk_band_at_interaction)


def build_digest(
    session: Session,
    risk_run_id: str,
    *,
    fa_assignment_source: FaAssignmentSource,
    cap_per_group: int,
    covering: dict[int, str] | None = None,
) -> DigestBuildResult:
    """Everyone routed to a call-queue or watch line in this run, grouped by
    FA (falling back to fund), tiered untouched-or-escalated before touched
    (see the module docstring), sorted by fund_at_risk within each tier, and
    split into cap_per_group-sized batches -- exactly the reads Section 16
    describes, plus the tiering. Nobody is left out of `lines`; batch says
    which cap_per_group-sized slice a row falls into.

    covering maps a client to the advisor standing in for their owner
    tonight. Those clients group under the stand-in, so the queue each
    advisor is handed is the one they will actually work, and the line
    records who they are covering for.
    """
    covering = covering or {}
    batch_size = max(cap_per_group, 1)
    snapshots = list(
        session.scalars(
            select(RiskSnapshot).where(
                RiskSnapshot.run_id == risk_run_id, RiskSnapshot.route.in_(DIGEST_ROUTES)
            )
        )
    )

    client_ids = sorted({row.client_id for row in snapshots})
    assignments = {
        (a.client_id, a.unit_fund_id): a.fa_id
        for a in fa_assignment_source.fetch_assignments(client_ids)
    }
    open_complaint_ids = set(
        session.scalars(
            select(ClientComplaint.client_id).where(
                ClientComplaint.client_id.in_(client_ids), ClientComplaint.status == "open"
            )
        )
    )
    latest_interactions = latest_interactions_for(
        session, sorted({(row.client_id, row.unit_fund_id) for row in snapshots})
    )

    by_group: dict[str, list[RiskSnapshot]] = defaultdict(list)
    covering_for: dict[tuple[int, int], str] = {}
    for row in snapshots:
        owner_fa_id = assignments.get((row.client_id, row.unit_fund_id))
        stand_in = covering.get(row.client_id) if owner_fa_id is not None else None
        if stand_in is not None and stand_in != owner_fa_id:
            covering_for[(row.client_id, row.unit_fund_id)] = owner_fa_id
        group_fa_id = stand_in if stand_in is not None else owner_fa_id
        by_group[group_key_for(group_fa_id, row.unit_fund_id)].append(row)

    groups: dict[str, DigestGroupData] = {}
    for key, rows in by_group.items():
        rows.sort(key=lambda r: (is_deprioritized(r, latest_interactions), -r.fund_at_risk))
        # The true total, over every eligible row -- everyone here ends up
        # in `lines`, batched, not just a capped first slice of them.
        total_fund_at_risk = sum(row.fund_at_risk for row in rows)
        lines = [
            DigestLineData(
                group_key=key,
                rank=rank,
                batch=(rank - 1) // batch_size,
                client_id=row.client_id,
                unit_fund_id=row.unit_fund_id,
                risk_score=row.risk_score,
                risk_band=row.risk_band,
                risk_reasons=row.risk_reasons,
                risk_reason_tags=fired_signal_tags(row),
                fund_at_risk=row.fund_at_risk,
                score_delta=delta_for(session, row.client_id, row.unit_fund_id, risk_run_id),
                route=row.route,
                in_call_queue=row.queue_rank is not None,
                complaint_caveat=row.client_id in open_complaint_ids,
                deprioritized=is_deprioritized(row, latest_interactions),
                covering_for_fa_id=covering_for.get((row.client_id, row.unit_fund_id)),
            )
            for rank, row in enumerate(rows, start=1)
        ]
        groups[key] = DigestGroupData(
            group_key=key,
            total_eligible=len(rows),
            total_fund_at_risk=total_fund_at_risk,
            lines=lines,
        )

    return DigestBuildResult(risk_run_id=risk_run_id, groups=groups)
