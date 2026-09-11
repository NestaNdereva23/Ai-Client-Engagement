"""Run one finding's fact filter again and report the count it gives today.

A fact on the screen is only trustworthy if the number behind it can be
checked. Every fact records the filter it was counted from. When that
filter names one of the watch list groups, the group is rebuilt against
today's data and the fresh count is returned beside the stored one.

Nothing here builds a query out of the stored JSON. A filter is either one
of the named watch list groups, or a list of conditions over the allow
listed fields the agent's own query tools use, which is compiled by the
same checked code that produced the fact in the first place. Any other
shape gets an honest refusal rather than an invented number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.agents.query_fields import FilterRefused
from app.agents.query_tools import measure_recorded_filter
from app.agents.watchlist import (
    GROUP_NAMES,
    WatchlistConfigMissing,
    build_watchlist,
    load_thresholds,
)
from app.db.models.agent_insight import AgentInsightFact

GROUP_NAME_KEY = "group_name"
CONDITIONS_KEY = "conditions"


@dataclass(frozen=True)
class RecountResult:
    """What the fact's filter counts today, or why it could not be run."""

    can_recount: bool
    as_of: date
    stored_value: str
    group_name: str | None = None
    client_count: int | None = None
    fund_count: int | None = None
    money_total_kes: float | None = None
    reason: str | None = None


def _group_name_for(fact: AgentInsightFact) -> str | None:
    """The watch list group this fact was counted from, if it names one."""
    named = (fact.source_filter or {}).get(GROUP_NAME_KEY)
    return named if named in GROUP_NAMES else None


def recount_fact(
    session: Session,
    fact: AgentInsightFact,
    *,
    as_of: date,
) -> RecountResult:
    """Count the fact's filter again as of the given day."""
    group_name = _group_name_for(fact)
    if group_name is None:
        return _recount_conditions(session, fact, as_of=as_of)

    try:
        thresholds = load_thresholds(session, as_of)
    except WatchlistConfigMissing:
        return RecountResult(
            can_recount=False,
            as_of=as_of,
            stored_value=fact.fact_value,
            group_name=group_name,
            reason=f"no risk config version is in force on {as_of.isoformat()}",
        )

    groups = build_watchlist(session, as_of, thresholds)
    group = next(item for item in groups if item.name == group_name)
    return RecountResult(
        can_recount=True,
        as_of=as_of,
        stored_value=fact.fact_value,
        group_name=group_name,
        client_count=group.client_count,
        fund_count=group.fund_count,
        money_total_kes=group.money_total,
    )


def _recount_conditions(session: Session, fact: AgentInsightFact, *, as_of: date) -> RecountResult:
    """Count a fact written by the agent's own query tools again."""
    conditions = (fact.source_filter or {}).get(CONDITIONS_KEY)
    if not isinstance(conditions, list):
        return RecountResult(
            can_recount=False,
            as_of=as_of,
            stored_value=fact.fact_value,
            reason=(
                "this fact's filter is neither a watch list group nor a list of "
                "conditions, so it cannot be run again"
            ),
        )

    try:
        measures = measure_recorded_filter(session, conditions)
    except FilterRefused as exc:
        return RecountResult(
            can_recount=False, as_of=as_of, stored_value=fact.fact_value, reason=str(exc)
        )

    if measures.get("too_small"):
        return RecountResult(
            can_recount=False,
            as_of=as_of,
            stored_value=fact.fact_value,
            reason="this filter now matches too few clients to report a count for",
        )
    return RecountResult(
        can_recount=True,
        as_of=as_of,
        stored_value=fact.fact_value,
        client_count=measures.get("client_count"),
        fund_count=measures.get("fund_count"),
        money_total_kes=measures.get("money_total_kes"),
    )
