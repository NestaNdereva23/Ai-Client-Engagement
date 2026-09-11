"""Listing, reading, deciding on, and re-checking what the agent found."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.insight_recount import RecountResult, recount_fact
from app.agents.insight_state import transition_insight
from app.db.models.agent_insight import AgentInsight, AgentInsightClient, AgentInsightFact
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor

_DECISION_TO_STATE = {"accept": "accepted", "dismiss": "dismissed"}


class InsightNotFound(Exception):
    """No agent_insight exists with the given id."""


class FactNotFound(Exception):
    """No fact with the given id belongs to that finding."""


def _insight_filters(
    *,
    run_id: int | None,
    insight_date: date | None,
    kind: str | None,
    state: str | None,
) -> list[Any]:
    clauses: list[Any] = []
    if run_id is not None:
        clauses.append(AgentInsight.run_id == run_id)
    if insight_date is not None:
        clauses.append(func.date(AgentInsight.created_at) == insight_date)
    if kind is not None:
        clauses.append(AgentInsight.kind == kind)
    if state is not None:
        clauses.append(AgentInsight.state == state)
    return clauses


def list_insights(
    session: Session,
    *,
    run_id: int | None = None,
    insight_date: date | None = None,
    kind: str | None = None,
    state: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[AgentInsight], str | None]:
    """One page of findings, newest first."""
    limit = clamp_limit(limit)
    query = select(AgentInsight).where(
        *_insight_filters(run_id=run_id, insight_date=insight_date, kind=kind, state=state)
    )
    if cursor is not None:
        query = query.where(AgentInsight.insight_id < decode_id_cursor(cursor))
    query = query.order_by(AgentInsight.insight_id.desc()).limit(limit + 1)

    rows = list(session.scalars(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].insight_id)
    return rows, next_cursor


def count_insights(
    session: Session,
    *,
    run_id: int | None = None,
    insight_date: date | None = None,
    kind: str | None = None,
    state: str | None = None,
) -> int:
    """How many findings list_insights' filters would return in total."""
    return session.scalar(
        select(func.count())
        .select_from(AgentInsight)
        .where(*_insight_filters(run_id=run_id, insight_date=insight_date, kind=kind, state=state))
    )


def count_insights_by_kind(
    session: Session,
    *,
    run_id: int | None = None,
    insight_date: date | None = None,
    state: str | None = None,
) -> dict[str, int]:
    """How many findings of each kind the same filters return, counted in
    the database so the screen's headline numbers never depend on the page.
    """
    rows = session.execute(
        select(AgentInsight.kind, func.count())
        .where(*_insight_filters(run_id=run_id, insight_date=insight_date, kind=None, state=state))
        .group_by(AgentInsight.kind)
    ).all()
    return {kind: count for kind, count in rows}


def get_insight(session: Session, insight_id: int) -> AgentInsight:
    """One finding, or raise InsightNotFound."""
    insight = session.get(AgentInsight, insight_id)
    if insight is None:
        raise InsightNotFound(insight_id)
    return insight


def get_insight_facts(session: Session, insight_id: int) -> list[AgentInsightFact]:
    """Every number the finding rests on, in the order they were written."""
    return list(
        session.scalars(
            select(AgentInsightFact)
            .where(AgentInsightFact.insight_id == insight_id)
            .order_by(AgentInsightFact.fact_id)
        ).all()
    )


def count_insight_clients(session: Session, insight_id: int) -> int:
    """How many client funds the finding actually lists."""
    return session.scalar(
        select(func.count())
        .select_from(AgentInsightClient)
        .where(AgentInsightClient.insight_id == insight_id)
    )


def decide_insight(
    session: Session,
    insight_id: int,
    *,
    decision: str,
    reason: str,
    decided_by: str,
) -> AgentInsight:
    """Accept or dismiss one finding, recording who decided and why."""
    insight = get_insight(session, insight_id)
    return transition_insight(
        session,
        insight,
        to_state=_DECISION_TO_STATE[decision],
        reason=reason,
        decided_by=decided_by,
    )


def get_insight_fact(session: Session, insight_id: int, fact_id: int) -> AgentInsightFact:
    """One fact belonging to the given finding, or raise FactNotFound."""
    fact = session.get(AgentInsightFact, fact_id)
    if fact is None or fact.insight_id != insight_id:
        raise FactNotFound(fact_id)
    return fact


def recheck_insight_fact(
    session: Session,
    insight_id: int,
    fact_id: int,
    *,
    as_of: date,
) -> RecountResult:
    """Run one fact's filter again and report what it counts today."""
    get_insight(session, insight_id)
    fact = get_insight_fact(session, insight_id, fact_id)
    return recount_fact(session, fact, as_of=as_of)
