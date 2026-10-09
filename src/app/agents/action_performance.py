from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Select, and_, case, delete, func, insert, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.action_performance import NONE_LABEL, UNKNOWN_LABEL, ActionPerformance
from app.db.models.action_result import ActionResult
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient, AgentProposalVariant
from app.db.models.llmops import GenerationRun
from app.db.models.outreach import OutreachMessage
from app.db.models.risk import RiskSnapshot

logger = structlog.get_logger(__name__)

ANCHOR_DAY = date(2024, 1, 1)


@dataclass(frozen=True)
class PerformanceOutcome:
    window_days: int
    rows: int


@dataclass(frozen=True)
class Cut:
    anchor: datetime
    period_hours: int
    window_days: int
    computed_at: datetime

    @property
    def period(self) -> timedelta:
        return timedelta(hours=self.period_hours)

    @property
    def last_complete_index(self) -> int:
        measurable_until = self.computed_at - timedelta(days=self.window_days)
        return (measurable_until - self.anchor) // self.period - 1

    def index_of(self, moment: datetime) -> int:
        return (moment - self.anchor) // self.period

    def start_of(self, index: int) -> datetime:
        return self.anchor + index * self.period


class Cell(NamedTuple):
    period_index: int
    action_code: str
    angle: str
    priority_tier: str
    risk_band: str
    content_mix: str
    variant: str


@dataclass
class Tally:
    sent: int = 0
    replied: int = 0
    opted_out: int = 0
    edited: int = 0
    deposited: int = 0
    money_in_kes: float = 0.0


def build_action_performance(
    session: Session,
    *,
    now: datetime | None = None,
    period_hours: int | None = None,
    windows: Sequence[int] | None = None,
) -> list[PerformanceOutcome]:
    settings = get_settings()
    computed_at = now or datetime.now(UTC)
    hours = period_hours if period_hours is not None else settings.action_performance_period_hours
    lengths = tuple(windows) if windows is not None else settings.action_result_windows
    anchor = _anchor(settings.db_timezone)

    outcomes = [
        PerformanceOutcome(
            window_days=length,
            rows=_rebuild_window(
                session,
                Cut(anchor=anchor, period_hours=hours, window_days=length, computed_at=computed_at),
            ),
        )
        for length in lengths
    ]
    record_audit(
        session,
        entity_type="action_performance",
        action="build",
        detail={
            "period_hours": hours,
            "windows": {str(outcome.window_days): outcome.rows for outcome in outcomes},
        },
    )
    session.commit()
    logger.info(
        "action_performance.built",
        period_hours=hours,
        windows={outcome.window_days: outcome.rows for outcome in outcomes},
    )
    return outcomes


def _anchor(zone_name: str) -> datetime:
    local_midnight = datetime.combine(ANCHOR_DAY, time.min, tzinfo=ZoneInfo(zone_name))
    return local_midnight.astimezone(UTC)


def _rebuild_window(session: Session, cut: Cut) -> int:
    complete_before = cut.start_of(cut.last_complete_index + 1)
    tallies = _tally_results(session, cut, complete_before)
    rows = [_row(cell, tally, cut) for cell, tally in tallies.items()]
    session.execute(
        delete(ActionPerformance).where(
            ActionPerformance.period_hours == cut.period_hours,
            ActionPerformance.window_days == cut.window_days,
        )
    )
    if rows:
        session.execute(insert(ActionPerformance), rows)
    return len(rows)


def _tally_results(session: Session, cut: Cut, complete_before: datetime) -> dict[Cell, Tally]:
    tallies: dict[Cell, Tally] = defaultdict(Tally)
    for row in session.execute(detail_query(cut.window_days, complete_before)):
        cell = Cell(
            period_index=cut.index_of(row.sent_at),
            action_code=row.action_code,
            angle=row.angle,
            priority_tier=row.priority_tier,
            risk_band=row.risk_band,
            content_mix=row.content_mix,
            variant=row.variant,
        )
        tally = tallies[cell]
        tally.sent += 1
        tally.replied += int(row.replied)
        tally.opted_out += int(row.opted_out)
        tally.edited += int(row.reviewer_changed)
        tally.deposited += int(row.deposited)
        tally.money_in_kes += row.deposit_amount_kes
    return tallies


def detail_query(window_days: int, complete_before: datetime) -> Select[Any]:
    band_at_send = (
        select(RiskSnapshot.risk_band)
        .where(
            RiskSnapshot.client_id == ActionResult.client_id,
            RiskSnapshot.unit_fund_id == ActionResult.unit_fund_id,
            RiskSnapshot.created_at <= ActionResult.sent_at,
        )
        .order_by(RiskSnapshot.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(
            ActionResult.sent_at,
            AgentProposal.group_name,
            ActionResult.replied,
            ActionResult.opted_out,
            ActionResult.reviewer_changed,
            ActionResult.deposited,
            ActionResult.deposit_amount_kes,
            AgentProposal.action_code,
            func.coalesce(AgentProposalVariant.angle, AgentProposal.angle, NONE_LABEL).label(
                "angle"
            ),
            func.coalesce(GenerationRun.priority_tier, UNKNOWN_LABEL).label("priority_tier"),
            func.coalesce(band_at_send, UNKNOWN_LABEL).label("risk_band"),
            func.coalesce(
                case(
                    (AgentProposalVariant.proposal_id.is_(None), AgentProposal.content_mix),
                    else_=AgentProposalVariant.content_mix,
                ),
                NONE_LABEL,
            ).label("content_mix"),
            func.coalesce(AgentProposalClient.variant, AgentProposal.variant, NONE_LABEL).label(
                "variant"
            ),
        )
        .join(AgentProposal, AgentProposal.proposal_id == ActionResult.proposal_id)
        .join(OutreachMessage, OutreachMessage.message_id == ActionResult.message_id)
        .outerjoin(GenerationRun, GenerationRun.run_id == OutreachMessage.generation_run_id)
        .outerjoin(
            AgentProposalClient,
            and_(
                AgentProposalClient.proposal_id == ActionResult.proposal_id,
                AgentProposalClient.client_id == ActionResult.client_id,
                AgentProposalClient.unit_fund_id == ActionResult.unit_fund_id,
            ),
        )
        .outerjoin(
            AgentProposalVariant,
            and_(
                AgentProposalVariant.proposal_id == ActionResult.proposal_id,
                AgentProposalVariant.variant == AgentProposalClient.variant,
            ),
        )
        .where(ActionResult.window_days == window_days, ActionResult.sent_at < complete_before)
    )


def _row(cell: Cell, tally: Tally, cut: Cut) -> dict[str, Any]:
    start = cut.start_of(cell.period_index)
    return {
        "period_start": start,
        "period_end": start + cut.period,
        "period_hours": cut.period_hours,
        "window_days": cut.window_days,
        "action_code": cell.action_code,
        "angle": cell.angle,
        "priority_tier": cell.priority_tier,
        "risk_band": cell.risk_band,
        "content_mix": cell.content_mix,
        "variant": cell.variant,
        "sent_count": tally.sent,
        "replied_count": tally.replied,
        "opted_out_count": tally.opted_out,
        "edited_count": tally.edited,
        "deposited_count": tally.deposited,
        "reply_rate": tally.replied / tally.sent,
        "opt_out_rate": tally.opted_out / tally.sent,
        "edit_rate": tally.edited / tally.sent,
        "deposit_rate": tally.deposited / tally.sent,
        "money_in_kes": tally.money_in_kes,
        "computed_at": cut.computed_at,
    }
