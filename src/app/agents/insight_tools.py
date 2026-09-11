"""The tools the agent uses to write down what it found.

Calling one of these is how a finding comes into being. Nothing here parses
a reply: the model writes a finding by calling write_insight, hangs each
number on it by calling add_fact, and says so plainly by calling
dismiss_group when a group turned out to hold nothing worth raising.

Two rules hold the whole thing up. A fact without the filter it came from is
refused, so every number on the screen can be run again and checked. And a
run may only write so many findings, so one noisy night cannot flood a
person's screen.

Nothing in this module proposes a message, starts a campaign or reaches the
mailer. Writing a finding down is the end of what the agent may do on its
own; a person decides what happens next.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.query_fields import (
    FIELD_NAMES,
    FUND_TABLE,
    RISK_TABLE,
    FilterRefused,
    compile_conditions,
)
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.agent_insight import (
    INSIGHT_CONFIDENCE_LEVELS,
    INSIGHT_KINDS,
    AgentInsight,
    AgentInsightFact,
)
from app.privacy.llm_client import ToolSpec

HISTORY_TABLE = "risk_snapshot"

# The tables a fact may say it was counted from.
SOURCE_TABLES: tuple[str, ...] = (RISK_TABLE, FUND_TABLE, HISTORY_TABLE)

WRITE_INSIGHT = "write_insight"
ADD_FACT = "add_fact"
DISMISS_GROUP = "dismiss_group"

INSIGHT_TOOL_NAMES: tuple[str, ...] = (WRITE_INSIGHT, ADD_FACT, DISMISS_GROUP)


def _refuse(error: str, message: str) -> dict[str, Any]:
    return {"error": error, "message": message}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _recorded_filter(conditions: Any) -> list[dict[str, Any]]:
    """The filter behind a number, checked against the allow list."""
    if not isinstance(conditions, list) or not conditions:
        raise FilterRefused("a fact needs the filter it came from: a non empty list of conditions")
    _, recorded = compile_conditions(conditions)
    return recorded


def insights_written(session: Session, run_id: int) -> int:
    """How many findings this run has already written."""
    written = session.scalar(
        select(func.count()).select_from(AgentInsight).where(AgentInsight.run_id == run_id)
    )
    return int(written or 0)


def make_insight_tools(
    *, run_id: int, dismissals: list[dict[str, Any]] | None = None
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Build the write tools for one agent run.

    Each one takes the session first and then the call's arguments, exactly
    like a registered read tool, so the executor dispatches it, scans what it
    returns and records the call the same way. Dismissals are collected into
    the list handed in, when one is, so the run's report can say where the
    agent looked and found nothing.
    """
    collected = dismissals if dismissals is not None else []

    def write_insight(
        session: Session,
        *,
        kind: str,
        title: str,
        group_name: str,
        client_count: int,
        suggestion: str,
        why_now: str,
        confidence: str,
        confidence_reason: str,
        group_definition: Any = None,
        money_total_kes: float | None = None,
        avoid_saying: str | None = None,
    ) -> dict[str, Any]:
        cap = get_settings().agent_insight_write_cap
        if insights_written(session, run_id) >= cap:
            return _refuse("write_cap_reached", f"this run has already written its {cap} findings")

        if kind not in INSIGHT_KINDS:
            return _refuse("unknown_kind", f"'{kind}' is not one of: {', '.join(INSIGHT_KINDS)}")
        if confidence not in INSIGHT_CONFIDENCE_LEVELS:
            return _refuse(
                "unknown_confidence",
                f"'{confidence}' is not one of: {', '.join(INSIGHT_CONFIDENCE_LEVELS)}",
            )

        required = {
            "title": title,
            "group_name": group_name,
            "suggestion": suggestion,
            "why_now": why_now,
            "confidence_reason": confidence_reason,
        }
        for name, value in required.items():
            if not _text(value):
                return _refuse("missing_text", f"{name} cannot be empty")

        if not isinstance(client_count, int) or isinstance(client_count, bool) or client_count < 0:
            return _refuse("bad_client_count", "client_count must be a whole number, zero or more")

        recorded: list[dict[str, Any]] | None = None
        if group_definition is not None:
            try:
                recorded = _recorded_filter(group_definition)
            except FilterRefused as exc:
                return _refuse("bad_filter", str(exc))

        insight = AgentInsight(
            run_id=run_id,
            kind=kind,
            title=_text(title),
            group_name=_text(group_name),
            group_definition=None if recorded is None else {"conditions": recorded},
            client_count=client_count,
            money_total_kes=money_total_kes,
            confidence=confidence,
            confidence_reason=_text(confidence_reason),
            suggestion=_text(suggestion),
            avoid_saying=_text(avoid_saying) or None,
            why_now=_text(why_now),
            state="new",
        )
        session.add(insight)
        session.flush()
        record_audit(
            session,
            entity_type="agent_insight",
            action="write",
            entity_id=str(insight.insight_id),
            run_id=str(run_id),
            detail={"kind": kind, "group_name": insight.group_name, "title": insight.title},
        )
        return {
            "status": "written",
            "insight_id": insight.insight_id,
            "written_this_run": insights_written(session, run_id),
            "cap": cap,
        }

    def add_fact(
        session: Session,
        *,
        insight_id: int,
        fact_text: str,
        fact_value: str,
        source_table: str,
        conditions: Any = None,
    ) -> dict[str, Any]:
        insight = session.get(AgentInsight, insight_id)
        if insight is None or insight.run_id != run_id:
            return _refuse("unknown_insight", f"this run has not written insight {insight_id}")
        if not _text(fact_text) or not _text(str(fact_value)):
            return _refuse("missing_text", "a fact needs both its wording and its number")
        if source_table not in SOURCE_TABLES:
            return _refuse(
                "unknown_source_table",
                f"'{source_table}' is not one of: {', '.join(SOURCE_TABLES)}",
            )
        try:
            recorded = _recorded_filter(conditions)
        except FilterRefused as exc:
            return _refuse("missing_filter", str(exc))

        fact = AgentInsightFact(
            insight_id=insight_id,
            fact_text=_text(fact_text),
            fact_value=_text(str(fact_value)),
            source_filter={"conditions": recorded},
            source_table=source_table,
        )
        session.add(fact)
        session.flush()
        record_audit(
            session,
            entity_type="agent_insight_fact",
            action="write",
            entity_id=str(fact.fact_id),
            run_id=str(run_id),
            detail={"insight_id": insight_id, "source_table": source_table},
        )
        return {"status": "recorded", "fact_id": fact.fact_id, "insight_id": insight_id}

    def dismiss_group(
        session: Session, *, group_name: str, reason: str, conditions: Any = None
    ) -> dict[str, Any]:
        if not _text(group_name):
            return _refuse("missing_text", "group_name cannot be empty")
        if not _text(reason):
            return _refuse("missing_reason", "say why this group holds nothing worth raising")

        recorded: list[dict[str, Any]] | None = None
        if conditions is not None:
            try:
                recorded = _recorded_filter(conditions)
            except FilterRefused as exc:
                return _refuse("bad_filter", str(exc))

        detail: dict[str, Any] = {"group_name": _text(group_name), "reason": _text(reason)}
        if recorded is not None:
            detail["conditions"] = recorded
        record_audit(
            session,
            entity_type="agent_insight",
            action="dismiss_group",
            run_id=str(run_id),
            detail=detail,
        )
        collected.append(dict(detail))
        return {"status": "noted", "group_name": _text(group_name)}

    return {WRITE_INSIGHT: write_insight, ADD_FACT: add_fact, DISMISS_GROUP: dismiss_group}


_CONDITIONS_SCHEMA = {
    "type": "array",
    "description": (
        "The filter this number came from: a list of conditions, all of which "
        f"must hold. Fields allowed: {', '.join(FIELD_NAMES)}."
    ),
    "items": {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": "One allowed field name."},
            "op": {"type": "string", "description": "One of the allowed operators."},
            "value": {"description": "The value to compare against."},
        },
        "required": ["field", "op"],
    },
}


def insight_tool_specs(*, cap: int | None = None) -> tuple[ToolSpec, ...]:
    """The specs offered to the model for writing findings down."""
    cap = get_settings().agent_insight_write_cap if cap is None else cap
    return (
        ToolSpec(
            name=WRITE_INSIGHT,
            description=(
                "Write down one finding. This is the only way a finding reaches a "
                f"person's screen. You may write at most {cap} in one run, so keep "
                "each one worth reading."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(INSIGHT_KINDS)},
                    "title": {"type": "string", "description": "One short line saying what it is."},
                    "group_name": {"type": "string", "description": "Who the finding is about."},
                    "group_definition": _CONDITIONS_SCHEMA,
                    "client_count": {"type": "integer", "description": "How many clients."},
                    "money_total_kes": {
                        "type": ["number", "null"],
                        "description": "The rounded money the group holds.",
                    },
                    "suggestion": {"type": "string", "description": "What you suggest doing."},
                    "avoid_saying": {
                        "type": ["string", "null"],
                        "description": "What a message about this must not claim.",
                    },
                    "why_now": {"type": "string", "description": "Why this matters today."},
                    "confidence": {"type": "string", "enum": sorted(INSIGHT_CONFIDENCE_LEVELS)},
                    "confidence_reason": {"type": "string", "description": "Why that confidence."},
                },
                "required": [
                    "kind",
                    "title",
                    "group_name",
                    "client_count",
                    "suggestion",
                    "why_now",
                    "confidence",
                    "confidence_reason",
                ],
            },
        ),
        ToolSpec(
            name=ADD_FACT,
            description=(
                "Hang one number on a finding you wrote, along with the filter it "
                "came from. A fact without its filter is refused."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "insight_id": {
                        "type": "integer",
                        "description": "The finding this belongs to.",
                    },
                    "fact_text": {"type": "string", "description": "What the number says."},
                    "fact_value": {"type": "string", "description": "The number itself."},
                    "source_table": {"type": "string", "enum": sorted(SOURCE_TABLES)},
                    "conditions": _CONDITIONS_SCHEMA,
                },
                "required": ["insight_id", "fact_text", "fact_value", "source_table", "conditions"],
            },
        ),
        ToolSpec(
            name=DISMISS_GROUP,
            description=(
                "Say that you looked at a group and found nothing worth raising, "
                "and why. This is kept as a record of where you looked."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_name": {"type": "string", "description": "The group you looked at."},
                    "reason": {"type": "string", "description": "Why it holds nothing tonight."},
                    "conditions": _CONDITIONS_SCHEMA,
                },
                "required": ["group_name", "reason"],
            },
        ),
    )
