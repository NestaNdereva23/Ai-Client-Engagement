"""The tools the agent uses to investigate a group on its own.

These prove the two things that matter about them: they answer real
questions from the real tables, and nothing they answer with can be traced
back to a person. A field outside the allow list, an operator that does not
suit a field, and a query that runs too long are all refused in plain
words rather than raising.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from app.agents.query_banding import round_money
from app.agents.query_tools import (
    QUERY_TOOL_NAMES,
    compare_slices,
    distribution,
    measure_slice,
    trend,
)
from app.agents.tool_runtime import make_async_query_executor
from app.config import get_settings
from app.db.async_session import AsyncSessionLocal, dispose_async_engine
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.session import SessionLocal

FUND_ID = 9744
CLIENT_IDS = tuple(range(974401, 974411))
RUN_IDS = ("query-tools-run-1", "query-tools-run-2")

# The whole seeded set is high risk, so a filter on risk_band matches it all
# and a filter on anything else can be checked against a known number.
BALANCES = (5_000.0, 12_000.0, 40_000.0, 88_000.0, 150_000.0, 260_000.0, 410_000.0, 900_000.0)


@pytest.fixture(autouse=True)
async def _dispose_async_engine_after_each_test():
    yield
    await dispose_async_engine()


def _clear() -> None:
    with SessionLocal() as session:
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(RUN_IDS)))
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(RUN_IDS)))
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.unit_fund_id == FUND_ID))
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.unit_fund_id == FUND_ID)
        )
        session.commit()


@pytest.fixture
def book(db: None):
    """Ten client funds on one fund, eight of them with a balance."""
    _clear()
    with SessionLocal() as session:
        for index, client_id in enumerate(CLIENT_IDS):
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    risk_band="High",
                    value_tier="Silver" if index % 2 else "Gold",
                    balance_tier="Small",
                    recency_band="Lapsed",
                    pattern_is_reliable=index % 2 == 0,
                    overdue_multiple=1.0 + index,
                    sig_heavy_withdrawal=False,
                    sig_dormant=index < 5,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=50 + index,
                    risk_reasons="dormant",
                    fund_at_risk=1_000.0 * (index + 1),
                    config_version=1,
                )
            )
        for index, balance in enumerate(BALANCES):
            session.add(
                ActiveClientFund(
                    client_id=CLIENT_IDS[index],
                    unit_fund_id=FUND_ID,
                    balance=balance,
                    n_deposits=index + 1,
                    n_withdrawals=0,
                    months_until_empty=float(index + 1),
                )
            )
        session.commit()

    yield

    _clear()


@pytest.fixture
def history(book: None):
    """Two completed risk runs holding the same book, a day apart."""
    with SessionLocal() as session:
        for offset, run_id in enumerate(RUN_IDS):
            session.add(
                RiskRun(
                    run_id=run_id,
                    state="completed",
                    reference_ts=datetime(2026, 1, 1 + offset, 6, 0),
                    config_version=1,
                )
            )
            for index, client_id in enumerate(CLIENT_IDS):
                session.add(
                    RiskSnapshot(
                        run_id=run_id,
                        client_id=client_id,
                        unit_fund_id=FUND_ID,
                        risk_band="High",
                        value_tier="Gold",
                        balance_tier="Small",
                        recency_band="Lapsed",
                        pattern_is_reliable=True,
                        overdue_multiple=1.0,
                        sig_heavy_withdrawal=False,
                        sig_dormant=True,
                        sig_broken_pattern=False,
                        sig_shrinking=False,
                        sig_going_dormant=False,
                        sig_never_repeated=False,
                        risk_score=50 + index + offset,
                        risk_reasons="dormant",
                        fund_at_risk=1_000.0 * (index + 1),
                        config_version=1,
                    )
                )
        session.commit()
    yield


HIGH_RISK = [{"field": "risk_band", "op": "eq", "value": "High"}]


async def test_measure_slice_counts_a_filter_and_carries_it(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(session, conditions=HIGH_RISK)

    assert result["measures"]["client_count"] >= len(CLIENT_IDS)
    assert result["source_table"] == "client_risk_features"
    assert result["source_filter"] == {"conditions": HIGH_RISK}


async def test_compare_slices_answers_both_sides(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await compare_slices(
            session,
            left=[*HIGH_RISK, {"field": "sig_dormant", "op": "is_true"}],
            right=[*HIGH_RISK, {"field": "sig_dormant", "op": "is_false"}],
            measures=["client_count"],
        )

    assert result["left"]["measures"]["client_count"] == 5
    assert result["right"]["measures"]["client_count"] == 5
    assert result["left"]["source_filter"] != result["right"]["source_filter"]


async def test_distribution_spreads_a_group_across_one_field(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await distribution(session, field="value_tier", conditions=HIGH_RISK)

    buckets = result["buckets"]
    assert sum(buckets.values()) >= len(CLIENT_IDS)
    assert set(buckets) <= {"Gold", "Silver", "unknown", "too small to show"}


async def test_distribution_bands_a_number_rather_than_listing_it(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await distribution(session, field="balance", conditions=HIGH_RISK)

    for label in result["buckets"]:
        assert not label.replace(".", "").isdigit()


async def test_trend_reads_the_last_few_runs(history: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await trend(session, measure="client_count", conditions=HIGH_RISK, periods=2)

    periods = [point["period"] for point in result["points"]]
    assert periods == sorted(periods)
    assert result["source_table"] == "risk_snapshot"


async def test_trend_refuses_a_field_history_does_not_keep(history: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await trend(
            session,
            measure="client_count",
            conditions=[{"field": "balance", "op": "gt", "value": 1}],
        )

    assert result["error"] == "filter_refused"
    assert "balance" in result["message"]


async def test_a_field_off_the_list_is_refused_by_name(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(
            session, conditions=[{"field": "client_code", "op": "eq", "value": "X"}]
        )

    assert result["error"] == "filter_refused"
    assert "client_code" in result["message"]


async def test_an_operator_that_does_not_suit_the_field_is_refused(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(
            session, conditions=[{"field": "risk_band", "op": "gt", "value": "High"}]
        )

    assert result["error"] == "filter_refused"
    assert "gt" in result["message"]


async def test_a_measure_off_the_list_is_refused(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(session, conditions=HIGH_RISK, measures=["client_name"])

    assert result["error"] == "filter_refused"
    assert "client_name" in result["message"]


async def test_an_empty_result_answers_zero_rather_than_withholding(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(
            session,
            conditions=[{"field": "risk_band", "op": "eq", "value": "no such band"}],
        )

    assert result["measures"]["too_small"] is False
    assert result["measures"]["client_count"] == 0


async def test_a_group_too_small_to_hide_in_is_withheld(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(
            session,
            conditions=[
                *HIGH_RISK,
                {"field": "risk_score", "op": "eq", "value": 50},
            ],
        )

    assert result["measures"]["too_small"] is True
    assert "client_count" not in result["measures"]


async def test_money_comes_back_rounded(book: None) -> None:
    async with AsyncSessionLocal() as session:
        result = await measure_slice(session, conditions=HIGH_RISK, measures=["money_total_kes"])

    total = result["measures"]["money_total_kes"]
    assert total == round_money(total)
    assert total % 1_000 == 0


async def test_a_slow_filter_comes_back_as_a_refusal(book: None, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise DBAPIError("select", {}, Exception("canceling statement due to statement timeout"))

    async with AsyncSessionLocal() as session:
        monkeypatch.setattr(session, "execute", boom)
        result = await measure_slice(session, conditions=HIGH_RISK)

    assert result["error"] == "timed_out"


def _numbers(value) -> list[float]:
    """Every number anywhere inside one tool answer."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [n for item in value.values() for n in _numbers(item)]
    if isinstance(value, list):
        return [n for item in value for n in _numbers(item)]
    return []


async def test_no_tool_output_carries_a_name_or_an_exact_figure(history: None) -> None:
    banned = ("client_id", "client_code", "client_name", "unit_fund_id")
    async with AsyncSessionLocal() as session:
        outputs = [
            await measure_slice(session, conditions=HIGH_RISK, measures=["money_total_kes"]),
            await compare_slices(session, left=HIGH_RISK, right=HIGH_RISK),
            await distribution(session, field="balance", conditions=HIGH_RISK),
            await trend(session, measure="money_at_risk_kes", conditions=HIGH_RISK),
        ]

    rendered = json.dumps(outputs)
    for word in banned:
        assert word not in rendered

    # Money is the only thing here big enough to identify an account, and
    # every money figure has been rounded, so none of them is a balance.
    money = [n for output in outputs for n in _numbers(output) if n >= 1_000]
    assert money
    for amount in money:
        assert amount == round_money(amount)
    for balance in BALANCES:
        assert balance not in money


@pytest.fixture
def run_id(db: None):
    with SessionLocal() as session:
        run = AgentRun(trigger="manual", state="running")
        session.add(run)
        session.commit()
        made = run.run_id
    yield made
    with SessionLocal() as session:
        session.execute(delete(AgentToolCall).where(AgentToolCall.run_id == made))
        session.execute(delete(AgentRun).where(AgentRun.run_id == made))
        session.commit()


async def test_every_call_is_recorded_against_the_run(book: None, run_id: int) -> None:
    async with AsyncSessionLocal() as session:
        call_tool = make_async_query_executor(session, run_id)
        await call_tool("measure_slice", {"conditions": HIGH_RISK})

    with SessionLocal() as session:
        rows = session.execute(
            text(
                "SELECT tool_name, tool_input, tool_output FROM agent_tool_call WHERE run_id = :r"
            ),
            {"r": run_id},
        ).all()

    assert [row.tool_name for row in rows] == ["measure_slice"]
    assert rows[0].tool_input == {"conditions": HIGH_RISK}
    assert rows[0].tool_output["source_filter"] == {"conditions": HIGH_RISK}


async def test_an_unknown_tool_name_is_refused_and_recorded(book: None, run_id: int) -> None:
    async with AsyncSessionLocal() as session:
        call_tool = make_async_query_executor(session, run_id)
        result = await call_tool("run_sql", {"query": "select 1"})

    assert result["error"] == "unknown_tool"
    with SessionLocal() as session:
        made = session.execute(
            text("SELECT count(*) AS n FROM agent_tool_call WHERE run_id = :r"), {"r": run_id}
        ).one()
    assert made.n == 1


async def test_a_run_cannot_spend_more_than_its_query_budget(
    book: None, run_id: int, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_QUERY_CALL_BUDGET", "2")
    get_settings.cache_clear()
    try:
        async with AsyncSessionLocal() as session:
            call_tool = make_async_query_executor(session, run_id)
            for _ in range(2):
                assert "error" not in await call_tool("measure_slice", {"conditions": HIGH_RISK})
            refused = await call_tool("measure_slice", {"conditions": HIGH_RISK})
    finally:
        get_settings.cache_clear()

    assert refused["error"] == "budget_spent"
    with SessionLocal() as session:
        made = session.execute(
            text("SELECT count(*) AS n FROM agent_tool_call WHERE run_id = :r"), {"r": run_id}
        ).one()
    assert made.n == 3


def test_the_query_tools_are_named_so_the_budget_can_find_them() -> None:
    assert set(QUERY_TOOL_NAMES) == {"measure_slice", "compare_slices", "distribution", "trend"}


def test_a_filter_may_not_be_longer_than_the_limit() -> None:
    from app.agents.query_fields import FilterRefused, compile_conditions

    conditions = [{"field": "risk_score", "op": "gt", "value": n} for n in range(50)]
    with pytest.raises(FilterRefused):
        compile_conditions(conditions)


def test_an_in_list_may_not_be_longer_than_the_limit() -> None:
    from app.agents.query_fields import FilterRefused, compile_conditions

    with pytest.raises(FilterRefused):
        compile_conditions(
            [{"field": "risk_band", "op": "in", "value": [str(n) for n in range(100)]}]
        )


def test_money_rounding_gets_coarser_as_the_number_grows() -> None:
    assert round_money(4_321) == 4_000
    assert round_money(123_456) == 120_000
    assert round_money(12_345_678) == 12_300_000
    assert round_money(None) is None


def test_no_date_field_is_on_the_allow_list() -> None:
    from app.agents.query_fields import FIELDS

    assert not [name for name in FIELDS if "date" in name]
