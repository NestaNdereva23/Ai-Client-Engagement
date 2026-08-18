"""Build one morning digest from a completed risk_run's snapshot rows.

Pure over already-fetched inputs, same discipline as risk/scoring.py and
risk/routing.py -- no persistence here. app/workers/digest.py calls this and
writes the result to digest_run/digest_line; app/services/digest.py reads
what was already written back out for the API.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.complaints import ClientComplaint
from app.db.models.risk import RiskSnapshot
from app.ingestion.fa_assignment_source import FaAssignmentSource
from app.risk.history import delta_for
from app.risk.signals import fired_signal_tags

# Routes a digest line ever exists for: the two an FA needs to see, in the
# order the notebook renders them (call queue first).
DIGEST_ROUTES = ("fa_call_priority", "fa_watchlist")


def group_key_for(fa_id: int | None, unit_fund_id: int) -> str:
    """ "fa:<fa_id>", or "fund:<unit_fund_id>" when there is no FA to group by."""
    return f"fa:{fa_id}" if fa_id is not None else f"fund:{unit_fund_id}"


@dataclass
class DigestLineData:
    """One client-fund's line, already ranked within its group."""

    group_key: str
    rank: int
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


def build_digest(
    session: Session,
    risk_run_id: str,
    *,
    fa_assignment_source: FaAssignmentSource,
    cap_per_group: int,
) -> DigestBuildResult:
    """Everyone routed to a call-queue or watch line in this run, grouped by
    FA (falling back to fund), sorted by fund_at_risk within each group, and
    capped -- exactly the reads Section 16 describes.
    """
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

    by_group: dict[str, list[RiskSnapshot]] = defaultdict(list)
    for row in snapshots:
        fa_id = assignments.get((row.client_id, row.unit_fund_id))
        by_group[group_key_for(fa_id, row.unit_fund_id)].append(row)

    groups: dict[str, DigestGroupData] = {}
    for key, rows in by_group.items():
        rows.sort(key=lambda r: r.fund_at_risk, reverse=True)
        # The true total, over every eligible row -- not just the ones the
        # cap below keeps in `lines`.
        total_fund_at_risk = sum(row.fund_at_risk for row in rows)
        capped = rows[:cap_per_group]
        lines = [
            DigestLineData(
                group_key=key,
                rank=rank,
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
            )
            for rank, row in enumerate(capped, start=1)
        ]
        groups[key] = DigestGroupData(
            group_key=key,
            total_eligible=len(rows),
            total_fund_at_risk=total_fund_at_risk,
            lines=lines,
        )

    return DigestBuildResult(risk_run_id=risk_run_id, groups=groups)
