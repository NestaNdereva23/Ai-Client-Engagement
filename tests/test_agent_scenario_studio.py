from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents.permissions import set_permission
from app.agents.scenario_studio import (
    AUTO_QUEUED,
    NEEDS_APPROVAL,
    NO_SITUATION_MATCHED,
    ScenarioInput,
    consolidation_winner,
    evaluate_scenario,
    matched_situations,
)
from app.agents.situations import (
    APPROACHING_PRESERVATION_LINE,
    DEPOSIT_SIZE_FALLING,
    FA_CALL_OVERDUE,
    FIRST_DEPOSIT_NO_REPEAT,
    FREQUENT_DEPOSITOR_ONE_FUND,
    GRADUAL_DEPOSITOR_DUE,
    RISK_ROSE_NO_CALL,
)
from app.agents.watchlist import WatchlistThresholds
from app.db.models.agent_permission import AgentPermission
from app.db.session import SessionLocal
from app.routing.tiers import TierThresholds

AS_OF = date(2026, 10, 13)

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)
TIER_THRESHOLDS = TierThresholds(
    version=1,
    review_period_days=365,
    recent_purchase_days=90,
    capital_preservation_threshold_kes=6_000.0,
)

ACT_ALONE_ACTION = "follow_up_when_no_one_called"


def _existing_permission(session) -> str | None:
    row = session.scalar(
        select(AgentPermission).where(
            AgentPermission.action_code == ACT_ALONE_ACTION,
            AgentPermission.priority_tier.is_(None),
            AgentPermission.risk_band.is_(None),
        )
    )
    return None if row is None else row.permission


@pytest.fixture
def act_alone_permission():
    with SessionLocal() as session:
        before = _existing_permission(session)
        set_permission(
            session,
            ACT_ALONE_ACTION,
            "act_alone",
            changed_by="scenario-studio-test",
            changed_reason="let the scenario studio test the auto-queue path",
        )
        session.commit()

    yield

    with SessionLocal() as session:
        if before is None:
            session.execute(
                delete(AgentPermission).where(
                    AgentPermission.action_code == ACT_ALONE_ACTION,
                    AgentPermission.priority_tier.is_(None),
                    AgentPermission.risk_band.is_(None),
                )
            )
        else:
            set_permission(
                session,
                ACT_ALONE_ACTION,
                before,
                changed_by="scenario-studio-test",
                changed_reason="restore the setting this test found in place",
            )
        session.commit()


def test_a_fresh_regular_client_matches_the_new_deposit_and_one_fund_situations() -> None:
    scenario = ScenarioInput(
        balance=50_000.0, days_since_last_deposit=10, n_deposits=1, first_deposit_days_ago=10
    )

    matched = matched_situations(scenario, THRESHOLDS, TIER_THRESHOLDS)

    assert set(matched) == {FIRST_DEPOSIT_NO_REPEAT, FREQUENT_DEPOSITOR_ONE_FUND}


def test_a_second_deposit_excludes_first_deposit_no_repeat() -> None:
    scenario = ScenarioInput(
        balance=50_000.0, days_since_last_deposit=10, n_deposits=2, first_deposit_days_ago=10
    )

    matched = matched_situations(scenario, THRESHOLDS, TIER_THRESHOLDS)

    assert FIRST_DEPOSIT_NO_REPEAT not in matched


def test_a_multi_match_scenario_matches_every_qualifying_situation() -> None:
    scenario = ScenarioInput(
        balance=200_000.0,
        days_since_last_deposit=200,
        typical_gap_days=60,
        n_deposits=4,
        sig_shrinking=True,
        advisor_call_overdue=True,
        risk_band_rose=True,
    )

    matched = matched_situations(scenario, THRESHOLDS, TIER_THRESHOLDS)

    assert set(matched) == {
        GRADUAL_DEPOSITOR_DUE,
        DEPOSIT_SIZE_FALLING,
        FA_CALL_OVERDUE,
        RISK_ROSE_NO_CALL,
    }


def test_a_scenario_matching_nothing() -> None:
    scenario = ScenarioInput(
        balance=200_000.0, days_since_last_deposit=30, n_deposits=4, funds_held=3
    )

    assert matched_situations(scenario, THRESHOLDS, TIER_THRESHOLDS) == ()


def test_a_balance_under_the_line_matches_nothing_even_with_a_call_overdue() -> None:
    scenario = ScenarioInput(
        balance=5_000.0, days_since_last_deposit=10, n_deposits=4, advisor_call_overdue=True
    )

    assert matched_situations(scenario, THRESHOLDS, TIER_THRESHOLDS) == ()


def test_consolidation_picks_the_highest_priority_situation() -> None:
    matched = (DEPOSIT_SIZE_FALLING, RISK_ROSE_NO_CALL, FREQUENT_DEPOSITOR_ONE_FUND)

    assert consolidation_winner(matched) == RISK_ROSE_NO_CALL


def test_consolidation_with_no_matches_has_no_winner() -> None:
    assert consolidation_winner(()) is None


def test_a_client_near_the_line_gets_an_advisor_task_and_yields_to_any_other_situation() -> None:
    scenario = ScenarioInput(
        balance=7_000.0,
        days_since_last_deposit=30,
        n_deposits=4,
        funds_held=3,
        near_preservation_line=True,
    )

    with SessionLocal() as session:
        result = evaluate_scenario(session, scenario, THRESHOLDS, as_of=AS_OF)

    assert result.matched == (APPROACHING_PRESERVATION_LINE,)
    assert result.winner == APPROACHING_PRESERVATION_LINE
    assert result.action_code == "give_advisor_a_task"
    assert consolidation_winner((APPROACHING_PRESERVATION_LINE, RISK_ROSE_NO_CALL)) == (
        RISK_ROSE_NO_CALL
    )


def test_a_client_with_an_overdue_call_auto_queues_within_the_ceiling(act_alone_permission) -> None:
    scenario = ScenarioInput(
        balance=50_000.0,
        days_since_last_deposit=30,
        n_deposits=4,
        funds_held=2,
        advisor_call_overdue=True,
    )

    with SessionLocal() as session:
        result = evaluate_scenario(
            session, scenario, THRESHOLDS, as_of=AS_OF, money_ceiling_kes=250_000.0
        )

    assert result.winner == FA_CALL_OVERDUE
    assert result.outcome == AUTO_QUEUED
    assert result.action_code == ACT_ALONE_ACTION


def test_a_balance_above_the_ceiling_needs_approval() -> None:
    scenario = ScenarioInput(balance=500_000.0, days_since_last_deposit=30, n_deposits=4)

    with SessionLocal() as session:
        result = evaluate_scenario(
            session, scenario, THRESHOLDS, as_of=AS_OF, money_ceiling_kes=250_000.0
        )

    assert result.winner == FREQUENT_DEPOSITOR_ONE_FUND
    assert result.outcome == NEEDS_APPROVAL
    assert "ceiling" in result.reason


def test_a_scenario_matching_nothing_reports_no_situation() -> None:
    scenario = ScenarioInput(
        balance=200_000.0, days_since_last_deposit=30, n_deposits=4, funds_held=3
    )

    with SessionLocal() as session:
        result = evaluate_scenario(session, scenario, THRESHOLDS, as_of=AS_OF)

    assert result.winner is None
    assert result.outcome == NO_SITUATION_MATCHED
    assert result.action_code is None
