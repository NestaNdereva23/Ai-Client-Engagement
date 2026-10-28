from __future__ import annotations

import pytest

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
