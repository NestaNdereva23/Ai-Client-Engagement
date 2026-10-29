from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.agents.tools as tools_module
from app.agents.campaign_agent import CHOOSE_TOUCH_PLAN_TOOL_NAME
from app.agents.campaign_suggester import SAVE_DRAFT_TOOL_NAME, build_audience_tool_spec
from app.agents.tools import (
    CAMPAIGN_TOOL_SPECS,
    CAMPAIGN_WRITE_TOOLS,
    PLANNING_TOOLS,
    REPORTING_TOOLS,
    SETUP_TOOLS,
    TOOL_FUNCTIONS,
    TOOL_SPECS,
    make_campaign_read_tools,
    specs_for,
)
from app.agents.web_research import WebSearchUnavailable
from app.agents.write_tools import WRITE_TOOL_SPECS
from app.schemas.campaigns import CampaignPlanConfig

READ_SETS = {
    "setup": SETUP_TOOLS,
    "planning": PLANNING_TOOLS,
    "reporting": REPORTING_TOOLS,
}


def _names(specs) -> set[str]:
    return {spec.name for spec in specs}


def _real_tools() -> set[str]:
    return _names(TOOL_SPECS) | _names(CAMPAIGN_TOOL_SPECS) | {build_audience_tool_spec().name}


@pytest.mark.parametrize("set_name", READ_SETS)
def test_a_tool_set_only_names_real_tools(set_name: str) -> None:
    assert set(READ_SETS[set_name]) <= _real_tools()


@pytest.mark.parametrize("set_name", READ_SETS)
def test_a_read_set_holds_no_write_tool(set_name: str) -> None:
    write_tools = set(CAMPAIGN_WRITE_TOOLS) | _names(WRITE_TOOL_SPECS)
    assert not set(READ_SETS[set_name]) & write_tools


def test_the_write_tool_names_match_the_tools_that_exist() -> None:
    assert set(CAMPAIGN_WRITE_TOOLS) == {CHOOSE_TOUCH_PLAN_TOOL_NAME, SAVE_DRAFT_TOOL_NAME}


def test_the_campaign_tools_stay_out_of_the_shared_registry() -> None:
    assert not _names(CAMPAIGN_TOOL_SPECS) & _names(TOOL_SPECS)
    assert not _names(CAMPAIGN_TOOL_SPECS) & set(TOOL_FUNCTIONS)


def test_every_planning_tool_has_an_implementation() -> None:
    bound = make_campaign_read_tools(
        plan_config=CampaignPlanConfig(), touch_points=0, allowed_angles=(), slots=()
    )
    assert set(PLANNING_TOOLS) <= set(TOOL_FUNCTIONS) | set(bound)


def test_the_planner_does_not_get_the_setup_or_write_only_tools() -> None:
    assert "get_waiting_audience" not in PLANNING_TOOLS
    assert "get_touch_history" not in SETUP_TOOLS


def test_specs_for_refuses_a_name_that_is_not_a_tool() -> None:
    with pytest.raises(KeyError):
        specs_for(("get_touch_history", "drop_everything"))


def test_the_planners_rate_answer_carries_a_citable_sentence_and_a_time(monkeypatch) -> None:
    row = SimpleNamespace(
        product="money market",
        rate_value=12.3,
        as_of=datetime(2026, 10, 27, 8, 0, tzinfo=UTC),
        source="https://example.com/rate",
    )
    monkeypatch.setattr(tools_module, "fetch_verified_rate", lambda session, product: row)

    answer = tools_module.citable_verified_rate(None, product="money market")

    assert answer["text"] == (
        "The money market fund's current rate is 12.3%, verified as of 2026-10-27."
    )
    assert answer["source"] == "https://example.com/rate"
    assert datetime.fromisoformat(answer["retrieved_at"]).tzinfo is not None


def test_the_shared_rate_answer_is_left_as_it_was(monkeypatch) -> None:
    row = SimpleNamespace(
        product="money market",
        rate_value=12.3,
        as_of=datetime(2026, 10, 27, tzinfo=UTC),
        source="x",
    )
    monkeypatch.setattr(tools_module, "fetch_verified_rate", lambda session, product: row)

    assert "text" not in tools_module.get_verified_rate(None, product="money market")


def test_the_planners_search_answer_is_stamped_and_a_failed_search_is_not(monkeypatch) -> None:
    hits = [SimpleNamespace(title="t", url="https://example.com/a", snippet="a snippet")]
    monkeypatch.setattr(tools_module, "search_verified_web", lambda settings, query, k: hits)
    stamped = tools_module.stamped_search_web(None, query="redeem")

    def down(settings, query, k):
        raise WebSearchUnavailable("search is down")

    monkeypatch.setattr(tools_module, "search_verified_web", down)
    failed = tools_module.stamped_search_web(None, query="redeem")

    assert stamped["results"][0]["snippet"] == "a snippet"
    assert "retrieved_at" in stamped
    assert failed["error"] == "web_search_unavailable"
    assert "retrieved_at" not in failed


def test_the_planner_binds_its_own_search_and_rate_tools() -> None:
    bound = make_campaign_read_tools(
        plan_config=CampaignPlanConfig(), touch_points=0, allowed_angles=(), slots=()
    )

    assert bound["search_web"] is tools_module.stamped_search_web
    assert bound["get_verified_rate"] is tools_module.citable_verified_rate
    assert TOOL_FUNCTIONS["search_web"] is tools_module.search_web
