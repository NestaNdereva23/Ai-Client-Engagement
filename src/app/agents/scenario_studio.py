"""Test one hypothetical client fund against the real situation rules.

A scenario never touches the database: it takes plain numbers, runs them
through the same boundary checks watchlist.py uses, the same priority order
propose.py uses to pick one winner, and the same permission gate
write_tools.py checks before anything sends. Nothing is written anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action
from app.agents.permissions import effective_permission
from app.agents.propose import GROUP_ACTIONS, SITUATION_PRIORITY
from app.agents.situation_action_mapping import action_code_for_situation
from app.agents.situations import HEALTHY_BANDS
from app.agents.watchlist import (
    FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
    FEE_PRESSURE_GONE_QUIET,
    GETTING_SMALLER,
    GROUP_TO_SITUATION,
    HEALTHY_ONE_FUND,
    MORE_URGENT_BUT_NOT_CALLED,
    SIGNED_UP_RECENTLY,
    VERY_SMALL_AND_QUIET,
    WAITING_ON_A_CALL,
    WatchlistThresholds,
)
from app.config import get_settings

ACT_ALONE = "act_alone"

AUTO_QUEUED = "auto_queued"
NEEDS_APPROVAL = "needs_approval"
NO_SITUATION_MATCHED = "no_situation_matched"


@dataclass(frozen=True)
class ScenarioInput:
    """One client fund's numbers, standing in for a real ActiveClientFund row."""

    balance: float
    n_deposits: int = 1
    first_deposit_days_ago: int | None = None
    months_until_empty: float | None = None
    sig_dormant: bool = False
    sig_shrinking: bool = False
    risk_band: str = "Watch"
    funds_held: int = 1
    days_since_call_flagged: int | None = None
    call_logged_since_flagged: bool = False
    route_moved_more_urgent: bool = False
    on_call_list: bool = False


@dataclass(frozen=True)
class ScenarioResult:
    matched: tuple[str, ...]
    winner: str | None
    folded: tuple[str, ...]
    action_code: str | None
    action_title: str | None
    permission: str | None
    outcome: str
    reason: str


def matched_situations(scenario: ScenarioInput, thresholds: WatchlistThresholds) -> tuple[str, ...]:
    """Every situation this scenario's numbers satisfy, in no particular order."""
    matched: list[str] = []
    if (
        scenario.n_deposits == 1
        and scenario.first_deposit_days_ago is not None
        and scenario.first_deposit_days_ago <= thresholds.new_client_days
    ):
        matched.append(SIGNED_UP_RECENTLY)
    if (
        scenario.months_until_empty is not None
        and scenario.months_until_empty < thresholds.months_until_empty
        and scenario.balance > 0
    ):
        if scenario.sig_dormant:
            matched.append(FEE_PRESSURE_GONE_QUIET)
        else:
            matched.append(FEE_PRESSURE_ACTIVE_CONTRIBUTOR)
    if scenario.balance < thresholds.small_balance and scenario.sig_dormant:
        matched.append(VERY_SMALL_AND_QUIET)
    if scenario.sig_shrinking:
        matched.append(GETTING_SMALLER)
    if scenario.risk_band in HEALTHY_BANDS and scenario.funds_held == 1:
        matched.append(HEALTHY_ONE_FUND)
    if (
        scenario.days_since_call_flagged is not None
        and scenario.days_since_call_flagged >= thresholds.awaiting_call_days
        and not scenario.call_logged_since_flagged
    ):
        matched.append(WAITING_ON_A_CALL)
    if scenario.route_moved_more_urgent and not scenario.on_call_list:
        matched.append(MORE_URGENT_BUT_NOT_CALLED)
    return tuple(matched)


def consolidation_winner(matched: tuple[str, ...]) -> str | None:
    """The one situation that wins when a scenario matches more than one."""
    for situation in SITUATION_PRIORITY:
        if situation in matched:
            return situation
    return None


def evaluate_scenario(
    session: Session,
    scenario: ScenarioInput,
    thresholds: WatchlistThresholds,
    *,
    as_of: date,
    money_ceiling_kes: float | None = None,
) -> ScenarioResult:
    """Run one scenario through matching, consolidation and the permission gate."""
    matched = matched_situations(scenario, thresholds)
    winner = consolidation_winner(matched)
    if winner is None:
        return ScenarioResult(
            matched=matched,
            winner=None,
            folded=(),
            action_code=None,
            action_title=None,
            permission=None,
            outcome=NO_SITUATION_MATCHED,
            reason="This client fund does not match any situation under the current thresholds.",
        )

    folded = tuple(situation for situation in matched if situation != winner)
    situation_code = GROUP_TO_SITUATION.get(winner, winner)
    action_code = action_code_for_situation(session, situation_code, as_of) or GROUP_ACTIONS.get(
        winner
    )
    action = load_action(session, action_code, as_of) if action_code else None
    ceiling = (
        money_ceiling_kes
        if money_ceiling_kes is not None
        else (action.money_ceiling_kes if action is not None else None)
    )
    permission = (
        effective_permission(
            session, action_code, money_total_kes=scenario.balance, money_ceiling_kes=ceiling
        )
        if action_code
        else None
    )
    outcome = AUTO_QUEUED if permission == ACT_ALONE else NEEDS_APPROVAL
    reason = _reason(scenario, ceiling=ceiling, permission=permission)

    return ScenarioResult(
        matched=matched,
        winner=winner,
        folded=folded,
        action_code=action_code,
        action_title=action.title if action is not None else None,
        permission=permission,
        outcome=outcome,
        reason=reason,
    )


def _reason(scenario: ScenarioInput, *, ceiling: float | None, permission: str | None) -> str:
    if get_settings().agent_force_approve_each:
        return (
            "The global kill switch is on, so every action needs approval regardless of situation."
        )
    if ceiling is not None and scenario.balance > ceiling:
        return (
            f"Balance (KES {scenario.balance:,.0f}) is above the KES {ceiling:,.0f} ceiling, "
            "so this needs approval instead of sending automatically."
        )
    if permission == ACT_ALONE:
        if ceiling is not None:
            return (
                f"Balance (KES {scenario.balance:,.0f}) is within the KES {ceiling:,.0f} "
                "ceiling, so this sends without a review step."
            )
        return "This action's permission level lets it send automatically."
    return f"This action's permission level is '{permission}', so it needs a person to approve it."
