"""Tonight's groups: one per active situation that has an action, read from the situation table.
A group is a fact, not a judgement, and never carries a client name."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.agents.fa_scope import narrow_to_scope
from app.agents.situations import (
    APPROACHING_PRESERVATION_LINE,
    DEPOSIT_SIZE_FALLING,
    FA_CALL_OVERDUE,
    FIRST_DEPOSIT_NO_REPEAT,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    RISK_ROSE_NO_CALL,
)
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.signals import ClientSituationState
from app.risk.store import load_active_config

GROUP_NAMES = (
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    DEPOSIT_SIZE_FALLING,
    FIRST_DEPOSIT_NO_REPEAT,
    FA_CALL_OVERDUE,
    RISK_ROSE_NO_CALL,
    APPROACHING_PRESERVATION_LINE,
)

GROUP_TO_SITUATION: dict[str, str] = {name: name for name in GROUP_NAMES}


class WatchlistConfigMissing(LookupError):
    """No risk config version is in force, so the thresholds cannot be read."""


@dataclass(frozen=True)
class WatchlistThresholds:
    """Every number the group filters need, from settings and the risk config in force."""

    new_client_days: int
    months_until_empty: float
    small_balance: float
    awaiting_call_days: int


@dataclass(frozen=True)
class GroupMember:
    """One client fund in a group. Pseudonymous keys and a balance only."""

    client_id: int
    unit_fund_id: int
    balance: float


@dataclass(frozen=True)
class WatchGroup:
    """One named group: who is in it, how many clients, and how much money."""

    name: str
    definition: dict = field(default_factory=dict)
    members: tuple[GroupMember, ...] = ()

    @property
    def client_count(self) -> int:
        return len({member.client_id for member in self.members})

    @property
    def fund_count(self) -> int:
        return len(self.members)

    @property
    def money_total(self) -> float:
        return sum(member.balance for member in self.members)


def load_thresholds(session: Session, as_of: date) -> WatchlistThresholds:
    """Read the thresholds in force on the given day: two agent settings, two risk config values."""
    config = load_active_config(session, as_of)
    if config is None:
        raise WatchlistConfigMissing(f"no risk config version is in force on {as_of}")
    settings = get_settings()
    return WatchlistThresholds(
        new_client_days=settings.agent_new_client_days,
        months_until_empty=float(config.thresholds["MONTHS_UNTIL_EMPTY"]),
        small_balance=float(config.thresholds["TINY_BALANCE"]),
        awaiting_call_days=settings.agent_awaiting_call_days,
    )


def _situation_statement(situation_code: str) -> Select:
    statement = (
        select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id, ActiveClientFund.balance)
        .join(
            ClientSituationState,
            (ClientSituationState.client_id == ActiveClientFund.client_id)
            & (ClientSituationState.unit_fund_id == ActiveClientFund.unit_fund_id),
        )
        .where(
            ClientSituationState.situation_code == situation_code,
            ClientSituationState.is_active.is_(True),
        )
    )
    return narrow_to_scope(statement, ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)


def _build_group(session: Session, name: str, definition: dict, statement: Select) -> WatchGroup:
    members = tuple(
        GroupMember(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=float(row.balance or 0.0),
        )
        for row in session.execute(statement).all()
    )
    return WatchGroup(name=name, definition=definition, members=members)


def situation_group(session: Session, name: str) -> WatchGroup:
    situation_code = GROUP_TO_SITUATION[name]
    definition = {"situation_code": situation_code, "is_active": True}
    return _build_group(session, name, definition, _situation_statement(situation_code))


def _group_filter(name: str) -> Callable[[Session, WatchlistThresholds, date], WatchGroup]:
    def build(session: Session, thresholds: WatchlistThresholds, as_of: date) -> WatchGroup:
        return situation_group(session, name)

    return build


GROUP_FILTERS = tuple(_group_filter(name) for name in GROUP_NAMES)


def build_watchlist(
    session: Session,
    as_of: date,
    thresholds: WatchlistThresholds | None = None,
) -> Sequence[WatchGroup]:
    """Run every group filter and return each group, empty ones included."""
    if thresholds is None:
        thresholds = load_thresholds(session, as_of)
    return tuple(build_group(session, thresholds, as_of) for build_group in GROUP_FILTERS)
