from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pattern_search import NOT_MESSAGED, OTHER_MESSAGES
from app.audit.log import record_audit
from app.db.models.agent_insight import (
    INSIGHT_CONFIDENCE_LEVELS,
    PATTERN_KIND,
    AgentInsight,
    AgentInsightFact,
)
from app.db.models.observed_pattern import ObservedPattern
from app.privacy.boundary import BoundaryAudit, run_conversation_boundary
from app.privacy.llm_client import ConversingLLMClient, LLMClientError, ToolSpec, as_converse_call
from app.privacy.scanners import InboundLeak, OutboundLeak

logger = structlog.get_logger(__name__)

READ_PATTERN = "read_pattern"
WRITE_PATTERN_INSIGHT = "write_pattern_insight"
MAX_TURNS = 5
RESULT_TABLE = "action_result"
LEFT_ALONE_TABLE = "agent_proposal_client"

SYSTEM_PROMPT = (
    "You help a person read one pattern found in how past messages to clients turned out. "
    "The pattern was found by counting. You did not find it. "
    f"First call {READ_PATTERN} to see the numbers. "
    f"Then call {WRITE_PATTERN_INSIGHT} once with your write up. "
    "Use only the numbers you were given. Do not invent reasons the numbers do not show. "
    "A pattern is something that was seen, not something that was proven, so say what was "
    "seen and what a person could look into. "
    "Never tell anyone to create or change a rule. "
    "Never describe any one client. "
    "Write in short, plain sentences."
)

OUTCOME_WORDS = {"deposited": "made a deposit", "replied": "replied"}
COMPARISON_WORDS = {
    OTHER_MESSAGES: "all the other messages sent",
    NOT_MESSAGED: "clients in the same groups who were left alone and sent nothing",
}


class PatternWriteup(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    why_now: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str = Field(min_length=1)
    avoid_saying: str | None = None


@dataclass(frozen=True)
class WriteUpResult:
    written: int
    failed: int


def tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name=READ_PATTERN,
            description="Read the numbers behind this pattern.",
            input_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name=WRITE_PATTERN_INSIGHT,
            description="Write up this pattern for a person to read. Call it once.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One short line saying what it is."},
                    "suggestion": {
                        "type": "string",
                        "description": "What a person could look into. Not a rule.",
                    },
                    "why_now": {"type": "string", "description": "Why this is worth a look."},
                    "confidence": {"type": "string", "enum": sorted(INSIGHT_CONFIDENCE_LEVELS)},
                    "confidence_reason": {"type": "string", "description": "Why that confidence."},
                    "avoid_saying": {
                        "type": ["string", "null"],
                        "description": "What a message about this must not claim.",
                    },
                },
                "required": ["title", "suggestion", "why_now", "confidence", "confidence_reason"],
            },
        ),
    )


def pattern_numbers(pattern: ObservedPattern) -> dict[str, Any]:
    return {
        "group": pattern.description,
        "direction": pattern.direction,
        "messages_in_group": pattern.sent_count,
        "outcome_measured": f"{OUTCOME_WORDS[pattern.outcome]} within {pattern.window_days} days",
        "percent_in_group": pattern.outcome_percent,
        "compared_with": COMPARISON_WORDS[pattern.compared_with],
        "size_of_comparison": pattern.comparison_count,
        "percent_in_comparison": pattern.comparison_percent,
        "gap_in_percentage_points": pattern.gap_points,
    }


def write_up_patterns(
    session: Session, client: ConversingLLMClient, *, seen_at: datetime
) -> WriteUpResult:
    pending = session.scalars(
        select(ObservedPattern)
        .where(
            ObservedPattern.insight_id.is_(None),
            ObservedPattern.status == "new",
            ObservedPattern.last_seen_at >= seen_at,
        )
        .order_by(ObservedPattern.pattern_id)
    ).all()
    written = 0
    for pattern in pending:
        if _write_up(session, client, pattern):
            written += 1
    return WriteUpResult(written=written, failed=len(pending) - written)


def _write_up(session: Session, client: ConversingLLMClient, pattern: ObservedPattern) -> bool:
    captured: list[PatternWriteup] = []

    def call_tool(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if name == READ_PATTERN:
            return pattern_numbers(pattern)
        if name == WRITE_PATTERN_INSIGHT:
            return _take_writeup(tool_input, captured)
        return {"error": "unknown_tool", "message": f"there is no tool called {name}"}

    try:
        run_conversation_boundary(
            {},
            as_converse_call(client, system=SYSTEM_PROMPT, tools=tool_specs()),
            call_tool,
            max_turns=MAX_TURNS,
            entity_id=str(pattern.pattern_id),
            audit=_audit_sink(session),
        )
    except (InboundLeak, OutboundLeak, LLMClientError) as exc:
        logger.warning("pattern_writeup.failed", pattern_id=pattern.pattern_id, reason=str(exc))
        session.commit()
        return False

    if not captured:
        session.commit()
        return False
    _save_insight(session, pattern, captured[-1])
    session.commit()
    return True


def _take_writeup(tool_input: dict[str, Any], captured: list[PatternWriteup]) -> dict[str, Any]:
    try:
        captured.append(PatternWriteup.model_validate(tool_input))
    except ValidationError as exc:
        problems = "; ".join(f"{error['loc'][0]}: {error['msg']}" for error in exc.errors())
        return {"error": "bad_writeup", "message": problems}
    return {"status": "written"}


def _audit_sink(session: Session):
    def sink(record: BoundaryAudit) -> None:
        record_audit(
            session,
            entity_type="observed_pattern",
            action="write_up_crossing",
            entity_id=record.entity_id,
            detail={
                "fields": record.fields,
                "inbound": record.inbound,
                "outbound": record.outbound,
                "reason": record.reason,
            },
        )

    return sink


def _save_insight(session: Session, pattern: ObservedPattern, writeup: PatternWriteup) -> None:
    insight = AgentInsight(
        kind=PATTERN_KIND,
        title=writeup.title,
        group_name=pattern.description,
        client_count=pattern.sent_count,
        confidence=writeup.confidence,
        confidence_reason=writeup.confidence_reason,
        suggestion=writeup.suggestion,
        avoid_saying=writeup.avoid_saying or None,
        why_now=writeup.why_now,
        state="new",
    )
    session.add(insight)
    session.flush()
    session.add_all(_facts(pattern, insight.insight_id))
    pattern.insight_id = insight.insight_id
    record_audit(
        session,
        entity_type="agent_insight",
        action="write",
        entity_id=str(insight.insight_id),
        detail={
            "kind": PATTERN_KIND,
            "pattern_id": pattern.pattern_id,
            "group_name": insight.group_name,
            "title": insight.title,
        },
    )


def _facts(pattern: ObservedPattern, insight_id: int) -> list[AgentInsightFact]:
    source_filter = {
        "features": pattern.features,
        "outcome": pattern.outcome,
        "compared_with": pattern.compared_with,
        "window_days": pattern.window_days,
    }
    comparison_table = LEFT_ALONE_TABLE if pattern.compared_with == NOT_MESSAGED else RESULT_TABLE
    outcome = OUTCOME_WORDS[pattern.outcome]
    comparison = COMPARISON_WORDS[pattern.compared_with]
    lines = [
        ("Messages sent to this group and measured", str(pattern.sent_count), RESULT_TABLE),
        (
            f"Share of this group that {outcome} within {pattern.window_days} days",
            f"{pattern.outcome_percent:g}%",
            RESULT_TABLE,
        ),
        (
            f"Share that {outcome} among {comparison}",
            f"{pattern.comparison_percent:g}%",
            comparison_table,
        ),
        ("Size of the comparison", str(pattern.comparison_count), comparison_table),
        ("Gap in percentage points", f"{pattern.gap_points:+g}", RESULT_TABLE),
    ]
    return [
        AgentInsightFact(
            insight_id=insight_id,
            fact_text=text,
            fact_value=value,
            source_filter=source_filter,
            source_table=table,
        )
        for text, value, table in lines
    ]
