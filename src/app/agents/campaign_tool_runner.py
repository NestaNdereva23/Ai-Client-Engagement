from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.tool_runtime import ToolCall, scanned_tool_output
from app.db.models.campaign_agent_run import CampaignAgentToolCall
from app.db.session import SessionLocal
from app.privacy.scanners import OutboundLeak

logger = structlog.get_logger(__name__)

CampaignTool = Callable[..., dict[str, Any]]


def _first_ordinal(session: Session, run_id: int) -> int:
    highest = session.scalar(
        select(func.coalesce(func.max(CampaignAgentToolCall.ordinal), 0)).where(
            CampaignAgentToolCall.run_id == run_id
        )
    )
    return highest + 1


def _save_call(run_id: int, ordinal: int, name: str, tool_input: dict, output: dict) -> None:
    # Its own session, so the trace survives a rollback of the run's work.
    with SessionLocal() as trace:
        trace.add(
            CampaignAgentToolCall(
                run_id=run_id,
                ordinal=ordinal,
                tool_name=name,
                tool_input=tool_input,
                tool_output=output,
            )
        )
        trace.commit()


def make_campaign_tool_runner(
    session: Session, run_id: int, tools: Mapping[str, CampaignTool]
) -> ToolCall:
    next_ordinal = [_first_ordinal(session, run_id)]

    def call_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        ordinal = next_ordinal[0]
        next_ordinal[0] += 1

        def done(output: dict[str, Any]) -> dict[str, Any]:
            _save_call(run_id, ordinal, tool_name, tool_input, output)
            return output

        function = tools.get(tool_name)
        if function is None:
            logger.warning("campaign_agent.unknown_tool", run_id=run_id, tool_name=tool_name)
            return done(
                {
                    "error": "unknown_tool",
                    "message": f"'{tool_name}' is not a tool this agent can call",
                }
            )
        try:
            output = function(session, **tool_input)
        except TypeError as exc:
            return done({"error": "invalid_input", "message": str(exc)})

        try:
            scanned_tool_output(output)
        except OutboundLeak:
            logger.error("campaign_agent.output_blocked", run_id=run_id, tool_name=tool_name)
            done({"error": "output_blocked", "message": "this tool's answer was withheld"})
            raise
        return done(output)

    return call_tool
