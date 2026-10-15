"""Test one hypothetical client against the real situation rules, never touching the database.
It uses the same tier rules, priority order and permission gate as a real run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action
from app.agents.permissions import effective_permission
from app.agents.propose import GROUP_ACTIONS, SITUATION_PRIORITY
from app.agents.situation_action_mapping import action_code_for_situation
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
from app.agents.watchlist import GROUP_TO_SITUATION, WatchlistThresholds
from app.config import get_settings
from app.routing.tiers import (
    ClientActivity,
    TierThresholds,
    classify_client_tier,
    load_tier_thresholds_in_force,
)

ACT_ALONE = "act_alone"

AUTO_QUEUED = "auto_queued"
NEEDS_APPROVAL = "needs_approval"
NO_SITUATION_MATCHED = "no_situation_matched"


@dataclass(frozen=True)
class ScenarioInput:
    """One client's numbers, standing in for a real client in the active book."""

    balance: float
    days_since_last_deposit: int | None = None
    typical_gap_days: float | None = None
    real_withdrawer: bool = False
    net_flow_negative: bool = False
    funds_held: int = 1
    n_deposits: int = 1
    first_deposit_days_ago: int | None = None
    sig_shrinking: bool = False
    advisor_call_overdue: bool = False
    risk_band_rose: bool = False
    near_preservation_line: bool = False


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


def matched_situations(
    scenario: ScenarioInput, thresholds: WatchlistThresholds, tier_thresholds: TierThresholds
) -> tuple[str, ...]:
    """Every situation this scenario's numbers satisfy, in no particular order."""
    activity = ClientActivity(
        client_id=0,
        total_balance_kes=scenario.balance,
        days_since_newest_deposit=scenario.days_since_last_deposit,
        days_since_newest_real_withdrawal=1 if scenario.real_withdrawer else None,
    )
    tier = classify_client_tier(activity, tier_thresholds)
    if tier.segment is None:
        return ()

    matched: list[str] = []
    past_gap = (
        scenario.typical_gap_days is not None
        and scenario.days_since_last_deposit is not None
        and scenario.days_since_last_deposit > scenario.typical_gap_days
    )
    if not tier.purchased_recently and not tier.real_withdrawer and past_gap:
        matched.append(GRADUAL_DEPOSITOR_DUE)
    if not tier.purchased_recently and tier.real_withdrawer:
        matched.append(GRADUAL_DEPOSITOR_WITHDRAWING)
    if tier.purchased_recently and not tier.real_withdrawer and scenario.funds_held == 1:
        matched.append(FREQUENT_DEPOSITOR_ONE_FUND)
    if tier.purchased_recently and tier.real_withdrawer and scenario.net_flow_negative:
        matched.append(FREQUENT_DEPOSITOR_WITHDRAWING)
    if scenario.sig_shrinking:
        matched.append(DEPOSIT_SIZE_FALLING)
    if (
        scenario.n_deposits == 1
        and scenario.first_deposit_days_ago is not None
        and scenario.first_deposit_days_ago <= thresholds.new_client_days
    ):
        matched.append(FIRST_DEPOSIT_NO_REPEAT)
    if scenario.advisor_call_overdue:
        matched.append(FA_CALL_OVERDUE)
    if scenario.risk_band_rose:
        matched.append(RISK_ROSE_NO_CALL)
    if scenario.near_preservation_line:
        matched.append(APPROACHING_PRESERVATION_LINE)
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
    matched = matched_situations(
        scenario, thresholds, load_tier_thresholds_in_force(session, as_of)
    )
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
