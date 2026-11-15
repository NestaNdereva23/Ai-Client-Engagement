from __future__ import annotations

from typing import Any

from app.agents.campaign_agent import build_choose_touch_plan_tool_spec
from app.agents.campaign_suggester import build_audience_tool_spec, build_save_draft_tool_spec
from app.agents.tools import PLANNING_TOOLS, SETUP_TOOLS, specs_for
from app.campaigns.agent_information import TOOL_SWITCHES
from app.privacy.llm_client import ToolSpec


def _entry(spec: ToolSpec, used_by: str) -> dict[str, Any]:
    return {
        "name": spec.name,
        "used_by": [used_by],
        "description": spec.description,
        "parameters": sorted(spec.input_schema.get("properties", {})),
        "switch": TOOL_SWITCHES.get(spec.name),
    }


def campaign_tool_catalog() -> list[dict[str, Any]]:
    planning = (
        *specs_for(PLANNING_TOOLS),
        build_choose_touch_plan_tool_spec(slot_numbers=[], allowed_angles=[]),
    )
    suggesting = (
        *specs_for(SETUP_TOOLS, extra=(build_audience_tool_spec(),)),
        build_save_draft_tool_spec(),
    )
    catalog: dict[str, dict[str, Any]] = {}
    for used_by, specs in (("Planning", planning), ("Suggesting", suggesting)):
        for spec in specs:
            if spec.name in catalog:
                catalog[spec.name]["used_by"].append(used_by)
            else:
                catalog[spec.name] = _entry(spec, used_by)
    return list(catalog.values())
