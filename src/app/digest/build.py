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


def fund_group_key_for(unit_fund_id: int) -> str:
    """ "fund:<unit_fund_id>", the whole fund's group."""
    return f"fund:{unit_fund_id}"


def is_fa_group(group_key: str) -> bool:
    """True for an advisor's own queue, false for a fund wide group."""
    return group_key.startswith("fa:")


@dataclass
class DigestLineData:
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
        if group_fa_id is not None:
            by_group[group_key_for(group_fa_id, row.unit_fund_id)].append(row)
        by_group[fund_group_key_for(row.unit_fund_id)].append(row)

    groups: dict[str, DigestGroupData] = {}
    for key, rows in by_group.items():
        rows.sort(key=lambda r: (is_deprioritized(r, latest_interactions), -r.fund_at_risk))
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
                covering_for_fa_id=(
                    covering_for.get((row.client_id, row.unit_fund_id))
                    if is_fa_group(key)
                    else None
                ),
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
