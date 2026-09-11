"""Which client funds a finding is actually about.

A finding is written in group terms: a name, a count, and sometimes the
filter it was found with. A proposal needs the real client funds behind it,
because every gate runs one client at a time and a client left out has to
carry a reason on the record.

There are two honest ways to get there and no third. When the finding names
one of the watch list groups, that group is rebuilt against today's data and
its members are used. Otherwise the finding's stored filter is compiled by
the same allow listed field code the agent's own query tools use. A finding
with neither is refused by name rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.agents.query_fields import FilterRefused, compile_conditions
from app.agents.watchlist import (
    GROUP_NAMES,
    GroupMember,
    WatchlistConfigMissing,
    build_watchlist,
    load_thresholds,
)
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.risk import ClientRiskFeatures

FROM_WATCH_LIST = "watch_list_group"
FROM_FILTER = "stored_filter"

CONDITIONS_KEY = "conditions"

_JOIN_ON = and_(
    ClientRiskFeatures.client_id == ActiveClientFund.client_id,
    ClientRiskFeatures.unit_fund_id == ActiveClientFund.unit_fund_id,
)


@dataclass(frozen=True)
class ResolvedMembers:
    """The client funds behind one finding, and where they came from."""

    members: tuple[GroupMember, ...]
    source: str | None = None
    refusal: str | None = None

    @property
    def resolved(self) -> bool:
        return self.refusal is None


def resolve_insight_members(
    session: Session, insight: AgentInsight, as_of: date
) -> ResolvedMembers:
    """The client funds one finding covers, or a plain reason there are none."""
    if insight.group_name in GROUP_NAMES:
        return _from_watch_list(session, insight, as_of)
    return _from_filter(session, insight)


def _from_watch_list(session: Session, insight: AgentInsight, as_of: date) -> ResolvedMembers:
    try:
        thresholds = load_thresholds(session, as_of)
    except WatchlistConfigMissing:
        return ResolvedMembers(
            members=(),
            refusal=f"no risk config version is in force on {as_of.isoformat()}",
        )

    groups = build_watchlist(session, as_of, thresholds)
    group = next((item for item in groups if item.name == insight.group_name), None)
    if group is None:
        return ResolvedMembers(
            members=(),
            refusal=f"'{insight.group_name}' is not one of today's groups",
        )
    return ResolvedMembers(members=group.members, source=FROM_WATCH_LIST)


def _from_filter(session: Session, insight: AgentInsight) -> ResolvedMembers:
    conditions = (insight.group_definition or {}).get(CONDITIONS_KEY)
    if not isinstance(conditions, list) or not conditions:
        return ResolvedMembers(
            members=(),
            refusal=(
                "this finding names no watch list group and stored no filter, so "
                "the clients it covers cannot be worked out"
            ),
        )

    try:
        clauses, _ = compile_conditions(conditions)
    except FilterRefused as exc:
        return ResolvedMembers(members=(), refusal=str(exc))

    rows = session.execute(
        select(
            ClientRiskFeatures.client_id,
            ClientRiskFeatures.unit_fund_id,
            ActiveClientFund.balance,
        )
        .select_from(ClientRiskFeatures)
        .outerjoin(ActiveClientFund, _JOIN_ON)
        .where(*clauses)
        .order_by(ClientRiskFeatures.client_id, ClientRiskFeatures.unit_fund_id)
    ).all()

    members = tuple(
        GroupMember(client_id=client_id, unit_fund_id=unit_fund_id, balance=float(balance or 0.0))
        for client_id, unit_fund_id, balance in rows
    )
    return ResolvedMembers(members=members, source=FROM_FILTER)
