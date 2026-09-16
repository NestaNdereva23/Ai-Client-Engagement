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
from app.agents.watchlist import (
    FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
    GETTING_SMALLER,
    HEALTHY_ONE_FUND,
    MORE_URGENT_BUT_NOT_CALLED,
    SIGNED_UP_RECENTLY,
    WAITING_ON_A_CALL,
    WatchlistThresholds,
)
from app.db.models.agent_permission import AgentPermission
from app.db.session import SessionLocal

AS_OF = date(2026, 9, 15)

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)

HEALTHY_ONE_FUND_ACTION = "suggest_second_fund"


def _existing_permission(session) -> str | None:
    row = session.scalar(
        select(AgentPermission).where(
            AgentPermission.action_code == HEALTHY_ONE_FUND_ACTION,
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
            HEALTHY_ONE_FUND_ACTION,
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
                    AgentPermission.action_code == HEALTHY_ONE_FUND_ACTION,
                    AgentPermission.priority_tier.is_(None),
                    AgentPermission.risk_band.is_(None),
                )
            )
        else:
            set_permission(
                session,
                HEALTHY_ONE_FUND_ACTION,
                before,
                changed_by="scenario-studio-test",
                changed_reason="restore the setting this test found in place",
            )
        session.commit()


def test_a_fresh_client_matches_signed_up_recently() -> None:
    scenario = ScenarioInput(balance=5_000.0, n_deposits=1, first_deposit_days_ago=10)

    matched = matched_situations(scenario, THRESHOLDS)

    assert matched == (SIGNED_UP_RECENTLY,)


def test_a_second_deposit_excludes_signed_up_recently() -> None:
    scenario = ScenarioInput(balance=5_000.0, n_deposits=2, first_deposit_days_ago=10)

    matched = matched_situations(scenario, THRESHOLDS)

    assert SIGNED_UP_RECENTLY not in matched


def test_a_multi_match_scenario_matches_every_qualifying_situation() -> None:
    scenario = ScenarioInput(
        balance=200_000.0,
        months_until_empty=3.0,
        days_since_call_flagged=6,
        call_logged_since_flagged=False,
        route_moved_more_urgent=True,
        on_call_list=False,
    )

    matched = matched_situations(scenario, THRESHOLDS)

    assert set(matched) == {
        FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
        WAITING_ON_A_CALL,
        MORE_URGENT_BUT_NOT_CALLED,
    }


def test_a_scenario_matching_nothing() -> None:
    scenario = ScenarioInput(balance=200_000.0, risk_band="High", funds_held=3)

    matched = matched_situations(scenario, THRESHOLDS)

    assert matched == ()


def test_consolidation_picks_the_highest_priority_situation() -> None:
    matched = (GETTING_SMALLER, MORE_URGENT_BUT_NOT_CALLED, HEALTHY_ONE_FUND)

    assert consolidation_winner(matched) == MORE_URGENT_BUT_NOT_CALLED


def test_consolidation_with_no_matches_has_no_winner() -> None:
    assert consolidation_winner(()) is None


def test_a_healthy_single_fund_client_auto_queues_within_the_ceiling(act_alone_permission) -> None:
    scenario = ScenarioInput(balance=50_000.0, risk_band="Low", funds_held=1)

    with SessionLocal() as session:
        result = evaluate_scenario(
            session, scenario, THRESHOLDS, as_of=AS_OF, money_ceiling_kes=250_000.0
        )

    assert result.winner == HEALTHY_ONE_FUND
    assert result.outcome == AUTO_QUEUED
    assert result.action_code is not None


def test_a_balance_above_the_ceiling_needs_approval() -> None:
    scenario = ScenarioInput(balance=500_000.0, risk_band="Low", funds_held=1)

    with SessionLocal() as session:
        result = evaluate_scenario(
            session, scenario, THRESHOLDS, as_of=AS_OF, money_ceiling_kes=250_000.0
        )

    assert result.winner == HEALTHY_ONE_FUND
    assert result.outcome == NEEDS_APPROVAL
    assert "ceiling" in result.reason


def test_a_scenario_matching_nothing_reports_no_situation() -> None:
    scenario = ScenarioInput(balance=200_000.0, risk_band="High", funds_held=3)

    with SessionLocal() as session:
        result = evaluate_scenario(session, scenario, THRESHOLDS, as_of=AS_OF)

    assert result.winner is None
    assert result.outcome == NO_SITUATION_MATCHED
    assert result.action_code is None
