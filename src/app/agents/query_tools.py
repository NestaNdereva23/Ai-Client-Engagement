"""Tools that let the agent ask its own questions about a group.

The agent sends a filter, not a query. Each tool below turns that filter
into one counting query over the allow listed fields in
app.agents.query_fields, and hands back sizes: how many clients, how much
money, how the group spreads, how a number has moved. No tool returns a
row, a name, an exact balance or an exact date, and every answer carries
the filter that produced it in the same shape a stored fact keeps, so a
figure on the screen can be run again and checked.

These read through the async session, because waiting on the database is
most of what an agent run spends its time doing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy import Select, and_, distinct, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.agents.query_banding import band_label, band_measures, is_too_small
from app.agents.query_fields import (
    CATEGORY,
    FIELD_NAMES,
    FLAG,
    FUND_TABLE,
    HISTORY_MEASURES,
    MEASURES,
    RISK_TABLE,
    FilterRefused,
    check_measures,
    compile_conditions,
    field_spec,
    touches_only_risk_fields,
)
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.privacy.llm_client import ToolSpec

logger = structlog.get_logger(__name__)

DEFAULT_MEASURES = ("client_count", "fund_count", "money_total_kes")

_MEASURE_EXPRESSIONS = {
    "client_count": func.count(distinct(ClientRiskFeatures.client_id)),
    "fund_count": func.count(),
    "money_total_kes": func.sum(ActiveClientFund.balance),
    "money_at_risk_kes": func.sum(ClientRiskFeatures.fund_at_risk),
    "avg_risk_score": func.avg(ClientRiskFeatures.risk_score),
}

_HISTORY_EXPRESSIONS = {
    "client_count": func.count(distinct(RiskSnapshot.client_id)),
    "fund_count": func.count(),
    "money_at_risk_kes": func.sum(RiskSnapshot.fund_at_risk),
    "avg_risk_score": func.avg(RiskSnapshot.risk_score),
}

_JOIN_ON = and_(
    ClientRiskFeatures.client_id == ActiveClientFund.client_id,
    ClientRiskFeatures.unit_fund_id == ActiveClientFund.unit_fund_id,
)


def _refusal(code: str, message: str) -> dict[str, Any]:
    return {"error": code, "message": message}


def _base(columns: Sequence[Any], clauses: Sequence[Any]) -> Select:
    """The one join these tools ever make, narrowed by the filter."""
    stmt = select(*columns).select_from(ClientRiskFeatures).outerjoin(ActiveClientFund, _JOIN_ON)
    return stmt.where(*clauses) if clauses else stmt


def _source(recorded: list[dict[str, Any]], table: str = RISK_TABLE) -> dict[str, Any]:
    return {"source_table": table, "source_filter": {"conditions": recorded}}


def _table_for(recorded: list[dict[str, Any]]) -> str:
    """Which table to name as the source, when a filter spans both."""
    tables = {field_spec(entry["field"]).table for entry in recorded}
    return FUND_TABLE if tables == {FUND_TABLE} else RISK_TABLE


class QueryTimedOut(Exception):
    """The database gave up on this filter before it finished."""

    def __init__(self, timeout_ms: int) -> None:
        super().__init__(timeout_ms)
        self.timeout_ms = timeout_ms


async def _fetch(session: AsyncSession, stmt: Select):
    """Run one counting query under a time limit of its own.

    A query that runs past the limit is cancelled by the database and comes
    back as a refusal, so one expensive filter cannot hold up a whole run.
    """
    timeout_ms = get_settings().agent_query_timeout_ms
    try:
        await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
        return (await session.execute(stmt)).one()
    except DBAPIError as exc:
        await session.rollback()
        logger.warning("agent_query.timed_out", error=str(exc.orig))
        raise QueryTimedOut(timeout_ms) from exc


async def _measure(
    session: AsyncSession, clauses: Sequence[Any], measures: Sequence[str]
) -> dict[str, Any]:
    """The asked for measures, plus the client count the banding needs."""
    wanted = tuple(dict.fromkeys(("client_count", *measures)))
    stmt = _base([_MEASURE_EXPRESSIONS[name] for name in wanted], clauses)
    row = await _fetch(session, stmt)
    raw = dict(zip(wanted, row, strict=True))
    client_count = int(raw["client_count"] or 0)
    asked = {name: raw[name] for name in measures}
    return band_measures(asked, client_count)


def measure_recorded_filter(
    session: Session, recorded: Sequence[dict[str, Any]], measures: Sequence[str] = DEFAULT_MEASURES
) -> dict[str, Any]:
    """The blocking twin of measure_slice, for a filter already recorded.

    A fact stores the filter that produced it, and the screen re-runs that
    filter through a blocking request. The compiling and the banding are
    the same code as the async path; only the fetch differs.
    """
    wanted = tuple(dict.fromkeys(("client_count", *check_measures(measures))))
    clauses, _ = compile_conditions(recorded)
    stmt = _base([_MEASURE_EXPRESSIONS[name] for name in wanted], clauses)
    raw = dict(zip(wanted, session.execute(stmt).one(), strict=True))
    return band_measures({name: raw[name] for name in measures}, int(raw["client_count"] or 0))


async def measure_slice(
    session: AsyncSession,
    *,
    conditions: list[dict[str, Any]] | None = None,
    measures: list[str] | None = None,
) -> dict[str, Any]:
    """How many clients and how much money match one filter."""
    try:
        wanted = check_measures(measures or list(DEFAULT_MEASURES))
        clauses, recorded = compile_conditions(conditions)
        values = await _measure(session, clauses, wanted)
    except FilterRefused as exc:
        return _refusal("filter_refused", str(exc))
    except QueryTimedOut as exc:
        return _refusal("timed_out", f"this filter took longer than {exc.timeout_ms}ms to count")
    return {"tool": "measure_slice", **_source(recorded, _table_for(recorded)), "measures": values}


async def compare_slices(
    session: AsyncSession,
    *,
    left: list[dict[str, Any]] | None = None,
    right: list[dict[str, Any]] | None = None,
    measures: list[str] | None = None,
) -> dict[str, Any]:
    """The same measures for two filters, side by side."""
    try:
        wanted = check_measures(measures or list(DEFAULT_MEASURES))
        left_clauses, left_recorded = compile_conditions(left)
        right_clauses, right_recorded = compile_conditions(right)
        left_values = await _measure(session, left_clauses, wanted)
        right_values = await _measure(session, right_clauses, wanted)
    except FilterRefused as exc:
        return _refusal("filter_refused", str(exc))
    except QueryTimedOut as exc:
        return _refusal("timed_out", f"this filter took longer than {exc.timeout_ms}ms to count")
    return {
        "tool": "compare_slices",
        "left": {**_source(left_recorded, _table_for(left_recorded)), "measures": left_values},
        "right": {**_source(right_recorded, _table_for(right_recorded)), "measures": right_values},
    }


def _bucket_expression(field: str):
    spec = field_spec(field)
    column = getattr(
        ClientRiskFeatures if spec.table == RISK_TABLE else ActiveClientFund, spec.name
    )
    return spec, column


async def distribution(
    session: AsyncSession,
    *,
    field: str,
    conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """How a group spreads across one banded field.

    A bucket holding too few clients to report on is not shown on its own.
    Those buckets are added together into one withheld line, so the shape
    of the group is still honest about what it left out.
    """
    try:
        spec, column = _bucket_expression(field)
        clauses, recorded = compile_conditions(conditions)
        stmt = _base(
            [column, func.count(distinct(ClientRiskFeatures.client_id))], clauses
        ).group_by(column)
        timeout_ms = get_settings().agent_query_timeout_ms
        try:
            await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            rows = (await session.execute(stmt)).all()
        except DBAPIError as exc:
            await session.rollback()
            raise QueryTimedOut(timeout_ms) from exc
    except FilterRefused as exc:
        return _refusal("filter_refused", str(exc))
    except QueryTimedOut as exc:
        return _refusal("timed_out", f"this filter took longer than {exc.timeout_ms}ms to count")

    buckets: dict[str, int] = {}
    for value, count in rows:
        label = _bucket_label(spec, value)
        buckets[label] = buckets.get(label, 0) + int(count or 0)

    shown = {label: count for label, count in buckets.items() if not is_too_small(count)}
    withheld = sum(count for label, count in buckets.items() if is_too_small(count))
    if withheld:
        shown["too small to show"] = withheld
    return {
        "tool": "distribution",
        "field": field,
        **_source(recorded, _table_for(recorded)),
        "buckets": shown,
    }


def _bucket_label(spec, value) -> str:
    if spec.kind == CATEGORY:
        return str(value) if value is not None else "unknown"
    if spec.kind == FLAG:
        return "yes" if value else "no"
    return band_label(spec.bands, value)


async def trend(
    session: AsyncSession,
    *,
    measure: str,
    conditions: list[dict[str, Any]] | None = None,
    periods: int = 6,
) -> dict[str, Any]:
    """How one measure has moved across the last few nightly runs.

    History keeps the risk numbers only, so a filter reaching into a
    client's activity row is refused here by name rather than answered
    from the wrong table.
    """
    settings = get_settings()
    try:
        check_measures([measure], HISTORY_MEASURES)
        clauses, recorded = compile_conditions(conditions, history=True)
        touches_only_risk_fields(recorded)
        wanted = max(1, min(int(periods), settings.agent_query_max_periods))
        runs = (
            await session.execute(
                select(RiskRun.run_id, RiskRun.reference_ts)
                .where(RiskRun.state == "completed")
                .order_by(RiskRun.reference_ts.desc())
                .limit(wanted)
            )
        ).all()
        points = []
        for run_id, reference_ts in reversed(runs):
            stmt = (
                select(
                    _HISTORY_EXPRESSIONS["client_count"],
                    _HISTORY_EXPRESSIONS[measure],
                )
                .select_from(RiskSnapshot)
                .where(RiskSnapshot.run_id == run_id, *clauses)
            )
            client_count, value = await _fetch(session, stmt)
            banded = band_measures({measure: value}, int(client_count or 0))
            points.append({"period": reference_ts.date().isoformat(), **banded})
    except FilterRefused as exc:
        return _refusal("filter_refused", str(exc))
    except QueryTimedOut as exc:
        return _refusal("timed_out", f"this filter took longer than {exc.timeout_ms}ms to count")
    return {
        "tool": "trend",
        "measure": measure,
        **_source(recorded, "risk_snapshot"),
        "points": points,
    }


QUERY_TOOL_FUNCTIONS = {
    "measure_slice": measure_slice,
    "compare_slices": compare_slices,
    "distribution": distribution,
    "trend": trend,
}

QUERY_TOOL_NAMES: tuple[str, ...] = tuple(QUERY_TOOL_FUNCTIONS)

_CONDITIONS_SCHEMA = {
    "type": "array",
    "description": (
        "The filter: a list of conditions, all of which must hold. "
        f"Fields allowed: {', '.join(FIELD_NAMES)}."
    ),
    "items": {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": "One allowed field name."},
            "op": {
                "type": "string",
                "description": (
                    "One of eq, ne, gt, gte, lt, lte, in, not_in, "
                    "is_null, not_null, is_true, is_false."
                ),
            },
            "value": {"description": "The value to compare against. Left out for is_ operators."},
        },
        "required": ["field", "op"],
    },
}

_MEASURES_SCHEMA = {
    "type": "array",
    "description": f"Which measures to return. One or more of: {', '.join(MEASURES)}.",
    "items": {"type": "string"},
}

QUERY_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="measure_slice",
        description=(
            "Count how many clients and how much money match a filter you write yourself. "
            "Answers come back as sizes and rounded money, never as rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conditions": _CONDITIONS_SCHEMA,
                "measures": _MEASURES_SCHEMA,
            },
        },
    ),
    ToolSpec(
        name="compare_slices",
        description="Measure two filters side by side, to see how one group differs from another.",
        input_schema={
            "type": "object",
            "properties": {
                "left": _CONDITIONS_SCHEMA,
                "right": _CONDITIONS_SCHEMA,
                "measures": _MEASURES_SCHEMA,
            },
        },
    ),
    ToolSpec(
        name="distribution",
        description="See how a group spreads across one field, as counts per band.",
        input_schema={
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "The field to spread the group across."},
                "conditions": _CONDITIONS_SCHEMA,
            },
            "required": ["field"],
        },
    ),
    ToolSpec(
        name="trend",
        description="See how one measure has moved across the last few nightly risk runs.",
        input_schema={
            "type": "object",
            "properties": {
                "measure": {
                    "type": "string",
                    "description": f"One of: {', '.join(HISTORY_MEASURES)}.",
                },
                "conditions": _CONDITIONS_SCHEMA,
                "periods": {"type": "integer", "description": "How many runs back to look."},
            },
            "required": ["measure"],
        },
    ),
)
