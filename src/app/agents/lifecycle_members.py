from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight, AgentInsightClient
from app.db.models.client_lifecycle import (
    ACTIVE_STATE,
    NO_POLICY,
    PENDING,
    WAITING_FOR_A_PERSON,
    AgentInsightLifecycle,
    ClientLifecycle,
)
from app.db.models.risk import ClientRiskFeatures

OPEN_OUTCOMES = (PENDING, WAITING_FOR_A_PERSON, NO_POLICY)


@dataclass(frozen=True)
class FundInState:
    client_id: int
    unit_fund_id: int
    balance: float

    @property
    def key(self) -> tuple[int, int]:
        return (self.client_id, self.unit_fund_id)


def funds_in_state(
    session: Session,
    *,
    state: str,
    clauses: Sequence[ColumnElement[bool]],
    among_insight: int | None = None,
) -> list[FundInState]:
    label = func.coalesce(ClientLifecycle.state, ACTIVE_STATE)
    query = (
        select(
            ClientRiskFeatures.client_id,
            ClientRiskFeatures.unit_fund_id,
            ActiveClientFund.balance,
        )
        .select_from(ClientRiskFeatures)
        .join(
            ActiveClientFund,
            and_(
                ActiveClientFund.client_id == ClientRiskFeatures.client_id,
                ActiveClientFund.unit_fund_id == ClientRiskFeatures.unit_fund_id,
            ),
        )
        .outerjoin(
            ClientLifecycle,
            and_(
                ClientLifecycle.client_id == ClientRiskFeatures.client_id,
                ClientLifecycle.unit_fund_id == ClientRiskFeatures.unit_fund_id,
            ),
        )
        .where(label == state, *clauses)
        .order_by(ClientRiskFeatures.client_id, ClientRiskFeatures.unit_fund_id)
    )
    if among_insight is not None:
        query = query.join(
            AgentInsightClient,
            and_(
                AgentInsightClient.insight_id == among_insight,
                AgentInsightClient.client_id == ClientRiskFeatures.client_id,
                AgentInsightClient.unit_fund_id == ClientRiskFeatures.unit_fund_id,
            ),
        )
    return [
        FundInState(client_id=client_id, unit_fund_id=unit_fund_id, balance=float(balance or 0.0))
        for client_id, unit_fund_id, balance in session.execute(query).all()
    ]


def funds_already_raised(
    session: Session, *, from_state: str, to_state: str
) -> set[tuple[int, int]]:
    rows = session.execute(
        select(AgentInsightClient.client_id, AgentInsightClient.unit_fund_id)
        .join(AgentInsight, AgentInsight.insight_id == AgentInsightClient.insight_id)
        .join(AgentInsightLifecycle, AgentInsightLifecycle.insight_id == AgentInsight.insight_id)
        .where(
            AgentInsight.state == "new",
            AgentInsightLifecycle.from_state == from_state,
            AgentInsightLifecycle.to_state == to_state,
            AgentInsightLifecycle.outcome.in_(OPEN_OUTCOMES),
        )
    ).all()
    return {(client_id, unit_fund_id) for client_id, unit_fund_id in rows}
