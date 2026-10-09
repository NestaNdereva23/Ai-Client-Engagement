from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.agents.guide_mix import MIX_LABELS
from app.agents.results_summary import percent
from app.config import get_settings
from app.db.models.action_result import ActionResult
from app.db.models.agent_proposal import AgentProposalClient, AgentProposalVariant
from app.services.agent_proposals import get_proposal

NO_GUIDE_LABEL = "No client guide"


class ProposalNotSplit(Exception):
    pass


class Counts(NamedTuple):
    measured: int = 0
    replied: int = 0
    opted_out: int = 0
    edited: int = 0
    deposited: int = 0
    money: float = 0.0


@dataclass(frozen=True)
class VersionSide:
    variant: str
    angle: str
    guide_mix: str | None
    guide_label: str
    clients: int
    measured: int
    replied: int
    opted_out: int
    edited: int
    deposited: int
    money_in_kes: float
    reply_percent: float | None
    opt_out_percent: float | None
    edit_percent: float | None
    deposit_percent: float | None


@dataclass(frozen=True)
class VersionComparison:
    proposal_id: int
    action_code: str
    group_name: str
    status: str
    differs_in: str
    window_days: int
    min_group_size: int
    enough_to_compare: bool
    sides: tuple[VersionSide, ...]


def compare_versions(
    session: Session, proposal_id: int, *, window_days: int | None = None
) -> VersionComparison:
    proposal = get_proposal(session, proposal_id)
    settings = get_settings()
    window = window_days or settings.action_performance_read_window_days
    definitions = list(
        session.scalars(
            select(AgentProposalVariant)
            .where(AgentProposalVariant.proposal_id == proposal_id)
            .order_by(AgentProposalVariant.variant)
        )
    )
    if not definitions:
        raise ProposalNotSplit(f"proposal {proposal_id} is not split into two versions")

    clients = dict(session.execute(_clients_query(proposal_id)).all())
    results = {row.variant: row for row in session.execute(_results_query(proposal_id, window))}
    sides = tuple(
        _side(
            definition,
            clients.get(definition.variant, 0),
            results.get(definition.variant, Counts()),
        )
        for definition in definitions
    )
    min_group_size = settings.agent_query_min_group_size
    return VersionComparison(
        proposal_id=proposal.proposal_id,
        action_code=proposal.action_code,
        group_name=proposal.group_name,
        status=proposal.status,
        differs_in="angle" if len({side.angle for side in sides}) > 1 else "guide",
        window_days=window,
        min_group_size=min_group_size,
        enough_to_compare=all(side.measured >= min_group_size for side in sides),
        sides=sides,
    )


def _clients_query(proposal_id: int) -> Select[Any]:
    return (
        select(
            AgentProposalClient.variant,
            func.count(func.distinct(AgentProposalClient.client_id)),
        )
        .where(
            AgentProposalClient.proposal_id == proposal_id,
            AgentProposalClient.included.is_(True),
            AgentProposalClient.variant.is_not(None),
        )
        .group_by(AgentProposalClient.variant)
    )


def _results_query(proposal_id: int, window_days: int) -> Select[Any]:
    return (
        select(
            AgentProposalClient.variant,
            func.count().label("measured"),
            func.count().filter(ActionResult.replied).label("replied"),
            func.count().filter(ActionResult.opted_out).label("opted_out"),
            func.count().filter(ActionResult.reviewer_changed).label("edited"),
            func.count().filter(ActionResult.deposited).label("deposited"),
            func.coalesce(func.sum(ActionResult.deposit_amount_kes), 0.0).label("money"),
        )
        .select_from(ActionResult)
        .join(
            AgentProposalClient,
            and_(
                AgentProposalClient.proposal_id == ActionResult.proposal_id,
                AgentProposalClient.client_id == ActionResult.client_id,
                AgentProposalClient.unit_fund_id == ActionResult.unit_fund_id,
            ),
        )
        .where(
            ActionResult.proposal_id == proposal_id,
            ActionResult.window_days == window_days,
            AgentProposalClient.variant.is_not(None),
        )
        .group_by(AgentProposalClient.variant)
    )


def _side(definition: AgentProposalVariant, clients: int, counts: Counts) -> VersionSide:
    return VersionSide(
        variant=definition.variant,
        angle=definition.angle,
        guide_mix=definition.content_mix,
        guide_label=MIX_LABELS.get(definition.content_mix or "", NO_GUIDE_LABEL),
        clients=clients,
        measured=counts.measured,
        replied=counts.replied,
        opted_out=counts.opted_out,
        edited=counts.edited,
        deposited=counts.deposited,
        money_in_kes=round(float(counts.money)),
        reply_percent=_rate(counts.replied, counts.measured),
        opt_out_percent=_rate(counts.opted_out, counts.measured),
        edit_percent=_rate(counts.edited, counts.measured),
        deposit_percent=_rate(counts.deposited, counts.measured),
    )


def _rate(count: int, measured: int) -> float | None:
    return percent(count, measured) if measured else None
