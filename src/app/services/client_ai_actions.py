from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.agents import card_copy
from app.db.models.active_clients import (
    AI_ACTION_INTERACTION_TYPES,
    AI_ACTION_SET_ASIDE,
    AI_ACTION_TAKEN,
    ActiveClientInteraction,
)
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient

OPEN_PROPOSAL_STATUS = "proposed"
RESPONSE_KIND_WITHOUT_A_SUGGESTION = "monitor_only"

CHOICE_BY_INTERACTION_TYPE = {AI_ACTION_TAKEN: "taken", AI_ACTION_SET_ASIDE: "set_aside"}


class AiActionNotFound(LookupError):
    pass


class AiActionNotOpen(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientAiAction:
    proposal_id: int
    action_code: str
    title: str
    response_kind: str | None
    response_label: str
    why_now: str
    group_label: str
    group_clients: int
    next_step: str
    choice: str | None
    created_at: datetime


def _copy_for_group(group_name: str, evidence: str) -> tuple[str, str]:
    try:
        copy = card_copy.copy_for(group_name)
    except card_copy.UnknownSituation:
        return evidence, group_name
    return copy.why_now, copy.screen_label


def _latest_open_proposal_per_client(session: Session, keys: Sequence[tuple[int, int]]):
    return session.execute(
        select(
            AgentProposalClient.client_id,
            AgentProposalClient.unit_fund_id,
            AgentProposal.proposal_id,
            AgentProposal.action_code,
            AgentProposal.catalog_version,
            AgentProposal.group_name,
            AgentProposal.evidence,
            AgentProposal.response_kind,
            AgentProposal.created_at,
        )
        .join(AgentProposal, AgentProposal.proposal_id == AgentProposalClient.proposal_id)
        .where(
            tuple_(AgentProposalClient.client_id, AgentProposalClient.unit_fund_id).in_(keys),
            AgentProposalClient.included.is_(True),
            AgentProposal.status == OPEN_PROPOSAL_STATUS,
            or_(
                AgentProposal.response_kind.is_(None),
                AgentProposal.response_kind != RESPONSE_KIND_WITHOUT_A_SUGGESTION,
            ),
        )
        .distinct(AgentProposalClient.client_id, AgentProposalClient.unit_fund_id)
        .order_by(
            AgentProposalClient.client_id,
            AgentProposalClient.unit_fund_id,
            AgentProposal.proposal_id.desc(),
        )
    ).all()


def _catalog_titles(session: Session, rows) -> dict[tuple[int, str], str]:
    versions_and_codes = {(row.catalog_version, row.action_code) for row in rows}
    return {
        (version, code): title
        for version, code, title in session.execute(
            select(
                AgentActionCatalog.version,
                AgentActionCatalog.action_code,
                AgentActionCatalog.title,
            ).where(
                tuple_(AgentActionCatalog.version, AgentActionCatalog.action_code).in_(
                    versions_and_codes
                )
            )
        )
    }


def _clients_in_each_proposal(session: Session, proposal_ids: set[int]) -> dict[int, int]:
    return dict(
        session.execute(
            select(AgentProposalClient.proposal_id, func.count())
            .where(
                AgentProposalClient.proposal_id.in_(proposal_ids),
                AgentProposalClient.included.is_(True),
            )
            .group_by(AgentProposalClient.proposal_id)
        ).all()
    )


def _latest_choices(
    session: Session, keys: Sequence[tuple[int, int]], proposal_ids: set[int]
) -> dict[tuple[int, int, int], str]:
    rows = session.execute(
        select(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            ActiveClientInteraction.proposal_id,
            ActiveClientInteraction.type,
        )
        .where(
            tuple_(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id).in_(
                keys
            ),
            ActiveClientInteraction.proposal_id.in_(proposal_ids),
            ActiveClientInteraction.type.in_(AI_ACTION_INTERACTION_TYPES),
        )
        .distinct(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            ActiveClientInteraction.proposal_id,
        )
        .order_by(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            ActiveClientInteraction.proposal_id,
            ActiveClientInteraction.id.desc(),
        )
    ).all()
    return {
        (row.client_id, row.unit_fund_id, row.proposal_id): CHOICE_BY_INTERACTION_TYPE[row.type]
        for row in rows
    }


def open_ai_actions_for(
    session: Session, keys: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], ClientAiAction]:
    if not keys:
        return {}
    rows = _latest_open_proposal_per_client(session, keys)
    if not rows:
        return {}

    titles = _catalog_titles(session, rows)
    proposal_ids = {row.proposal_id for row in rows}
    group_sizes = _clients_in_each_proposal(session, proposal_ids)
    choices = _latest_choices(session, keys, proposal_ids)

    actions: dict[tuple[int, int], ClientAiAction] = {}
    for row in rows:
        why_now, group_label = _copy_for_group(row.group_name, row.evidence)
        title = titles.get((row.catalog_version, row.action_code)) or row.action_code
        actions[(row.client_id, row.unit_fund_id)] = ClientAiAction(
            proposal_id=row.proposal_id,
            action_code=row.action_code,
            title=title,
            response_kind=row.response_kind,
            response_label=card_copy.response_label_for(row.response_kind),
            why_now=why_now,
            group_label=group_label,
            group_clients=group_sizes.get(row.proposal_id, 0),
            next_step=card_copy.next_step_for(row.response_kind),
            choice=choices.get((row.client_id, row.unit_fund_id, row.proposal_id)),
            created_at=row.created_at,
        )
    return actions


def require_open_ai_action(
    session: Session, proposal_id: int | None, client_id: int, unit_fund_id: int
) -> None:
    if proposal_id is None:
        raise AiActionNotFound(f"no proposal named for {client_id}/{unit_fund_id}")
    status = session.scalar(
        select(AgentProposal.status)
        .join(AgentProposalClient, AgentProposalClient.proposal_id == AgentProposal.proposal_id)
        .where(
            AgentProposal.proposal_id == proposal_id,
            AgentProposalClient.client_id == client_id,
            AgentProposalClient.unit_fund_id == unit_fund_id,
            AgentProposalClient.included.is_(True),
        )
    )
    if status is None:
        raise AiActionNotFound(f"{proposal_id} for {client_id}/{unit_fund_id}")
    if status != OPEN_PROPOSAL_STATUS:
        raise AiActionNotOpen(f"{proposal_id} is {status}")
