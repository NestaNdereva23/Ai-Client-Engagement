from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.agents.events import NO_EVENTS, EventLog
from app.agents.insight_tools import InsightWriteBudget, insights_written
from app.agents.lifecycle_members import funds_already_raised, funds_in_state
from app.agents.query_banding import round_money
from app.agents.query_fields import FIELD_NAMES, RISK_TABLE, FilterRefused, compile_conditions
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.agent_event import INSIGHT_CREATED
from app.db.models.agent_insight import (
    INSIGHT_CONFIDENCE_LEVELS,
    LIFECYCLE_CHANGE_KIND,
    AgentInsight,
    AgentInsightClient,
    AgentInsightFact,
)
from app.db.models.client_lifecycle import LIFECYCLE_STATES, PENDING, AgentInsightLifecycle
from app.privacy.llm_client import ToolSpec

WRITE_LIFECYCLE_CHANGE = "write_lifecycle_change"

LIFECYCLE_TOOL_NAMES: tuple[str, ...] = (WRITE_LIFECYCLE_CHANGE,)


def _refuse(error: str, message: str) -> dict[str, Any]:
    return {"error": error, "message": message}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def make_lifecycle_tools(
    *,
    run_id: int,
    budget: InsightWriteBudget | None = None,
    events: EventLog = NO_EVENTS,
) -> dict[str, Callable[..., dict[str, Any]]]:
    def write_lifecycle_change(
        session: Session,
        *,
        from_state: str,
        to_state: str,
        title: str,
        why_now: str,
        confidence: str,
        confidence_reason: str,
        conditions: Any = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        cap = settings.agent_insight_write_cap if budget is None else budget.cap
        spent = budget.spent if budget is not None else insights_written(session, run_id) >= cap
        if spent:
            return _refuse("write_cap_reached", f"this run has already written its {cap} findings")

        if from_state not in LIFECYCLE_STATES or to_state not in LIFECYCLE_STATES:
            return _refuse("unknown_state", f"states must be among: {', '.join(LIFECYCLE_STATES)}")
        if from_state == to_state:
            return _refuse("not_a_change", "the two states are the same")
        if confidence not in INSIGHT_CONFIDENCE_LEVELS:
            return _refuse(
                "unknown_confidence",
                f"'{confidence}' is not one of: {', '.join(INSIGHT_CONFIDENCE_LEVELS)}",
            )
        for name, value in (
            ("title", title),
            ("why_now", why_now),
            ("confidence_reason", confidence_reason),
        ):
            if not _text(value):
                return _refuse("missing_text", f"{name} cannot be empty")
        if not isinstance(conditions, list) or not conditions:
            return _refuse("missing_filter", "name the clients with a non empty list of conditions")

        try:
            clauses, recorded = compile_conditions(conditions)
        except FilterRefused as exc:
            return _refuse("bad_filter", str(exc))

        raised = funds_already_raised(session, from_state=from_state, to_state=to_state)
        funds = [
            fund
            for fund in funds_in_state(session, state=from_state, clauses=clauses)
            if fund.key not in raised
        ]
        if not funds:
            return _refuse(
                "no_clients",
                f"no client fund labelled {from_state} matches this filter, or each one is "
                "already in an open finding for this change",
            )
        if len(funds) > settings.lifecycle_insight_max_clients:
            return _refuse(
                "too_many_clients",
                f"the filter matches {len(funds)} client funds, and one finding may name at most "
                f"{settings.lifecycle_insight_max_clients}. Narrow the filter.",
            )
        if budget is not None and not budget.take():
            return _refuse("write_cap_reached", f"this run has already written its {cap} findings")

        insight = AgentInsight(
            run_id=run_id,
            kind=LIFECYCLE_CHANGE_KIND,
            title=_text(title),
            group_name=f"{from_state}_to_{to_state}",
            group_definition={"conditions": recorded},
            client_count=len({fund.client_id for fund in funds}),
            money_total_kes=round_money(sum(fund.balance for fund in funds)),
            confidence=confidence,
            confidence_reason=_text(confidence_reason),
            suggestion=(
                f"Change these clients from {from_state} to {to_state} if the written rule "
                "allows it."
            ),
            why_now=_text(why_now),
            state="new",
        )
        session.add(insight)
        session.flush()
        session.add_all(
            AgentInsightClient(
                insight_id=insight.insight_id,
                client_id=fund.client_id,
                unit_fund_id=fund.unit_fund_id,
            )
            for fund in funds
        )
        session.add(
            AgentInsightLifecycle(
                insight_id=insight.insight_id,
                from_state=from_state,
                to_state=to_state,
                outcome=PENDING,
            )
        )
        session.add(
            AgentInsightFact(
                insight_id=insight.insight_id,
                fact_text=f"client funds labelled {from_state} that match the filter",
                fact_value=str(len(funds)),
                source_filter={"conditions": recorded},
                source_table=RISK_TABLE,
            )
        )
        session.flush()
        record_audit(
            session,
            entity_type="agent_insight",
            action="write",
            entity_id=str(insight.insight_id),
            run_id=str(run_id),
            detail={
                "kind": LIFECYCLE_CHANGE_KIND,
                "group_name": insight.group_name,
                "title": insight.title,
            },
        )
        events.record(
            INSIGHT_CREATED,
            insight_id=insight.insight_id,
            insight_kind=LIFECYCLE_CHANGE_KIND,
            group_name=insight.group_name,
            client_count=insight.client_count,
            confidence=confidence,
        )
        return {
            "status": "written",
            "insight_id": insight.insight_id,
            "client_funds": len(funds),
            "note": "Nothing has changed. The written rule decides whether the label changes.",
        }

    return {WRITE_LIFECYCLE_CHANGE: write_lifecycle_change}


def lifecycle_tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name=WRITE_LIFECYCLE_CHANGE,
            description=(
                "Say that some clients no longer behave like their label, for example clients "
                "labelled active who have stopped paying in. This only writes a finding. You "
                "never change a label. A written rule decides whether it changes, and some "
                "rules wait for a person. Name the clients with a filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_state": {"type": "string", "enum": list(LIFECYCLE_STATES)},
                    "to_state": {"type": "string", "enum": list(LIFECYCLE_STATES)},
                    "title": {"type": "string", "description": "One short line saying what it is."},
                    "why_now": {"type": "string", "description": "Why this matters today."},
                    "confidence": {"type": "string", "enum": sorted(INSIGHT_CONFIDENCE_LEVELS)},
                    "confidence_reason": {"type": "string", "description": "Why that confidence."},
                    "conditions": {
                        "type": "array",
                        "description": (
                            "The filter that names the clients: a list of conditions, all of "
                            f"which must hold. Fields allowed: {', '.join(FIELD_NAMES)}."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "One allowed field."},
                                "op": {"type": "string", "description": "One allowed operator."},
                                "value": {"description": "The value to compare against."},
                            },
                            "required": ["field", "op"],
                        },
                    },
                },
                "required": [
                    "from_state",
                    "to_state",
                    "title",
                    "why_now",
                    "confidence",
                    "confidence_reason",
                    "conditions",
                ],
            },
        ),
    )
