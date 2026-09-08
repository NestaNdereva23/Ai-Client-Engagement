"""What the agent is allowed to ask about, and how it may ask.

The agent never writes SQL. It sends a list of conditions, each naming a
field, an operator and a value, and this module turns that into a query.
A field that is not on the allow list below is refused by name, and so is
an operator that does not suit the field. Nothing here accepts a raw
expression, a subquery, or a join the model picked: the only join is the
fixed one between a client fund's risk row and its activity row, on the
client and fund together.

Client ids, client codes and every exact date are left off the list on
purpose. They are how a row becomes a person.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement

from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.risk import ClientRiskFeatures, RiskSnapshot

RISK_TABLE = "client_risk_features"
FUND_TABLE = "active_client_fund"

CATEGORY = "category"
NUMBER = "number"
FLAG = "flag"

OPERATORS_BY_KIND: dict[str, tuple[str, ...]] = {
    CATEGORY: ("eq", "ne", "in", "not_in", "is_null", "not_null"),
    NUMBER: ("eq", "ne", "gt", "gte", "lt", "lte", "is_null", "not_null"),
    FLAG: ("is_true", "is_false"),
}

# Operators that take no value at all.
VALUELESS_OPERATORS = ("is_null", "not_null", "is_true", "is_false")

# Operators that take a list rather than a single value.
LIST_OPERATORS = ("in", "not_in")


class FilterRefused(Exception):
    """The filter cannot be run as written. The message is for the model."""


@dataclass(frozen=True)
class FieldSpec:
    """One field the agent may filter on, and how it may be banded."""

    name: str
    table: str
    kind: str
    # Cut points for distribution, low to high. Only numbers carry these.
    bands: tuple[float, ...] = ()
    # The matching column on risk_snapshot, when this field has history.
    has_history: bool = False


_RISK_FIELDS = (
    FieldSpec("risk_band", RISK_TABLE, CATEGORY, has_history=True),
    FieldSpec("value_tier", RISK_TABLE, CATEGORY, has_history=True),
    FieldSpec("balance_tier", RISK_TABLE, CATEGORY, has_history=True),
    FieldSpec("recency_band", RISK_TABLE, CATEGORY, has_history=True),
    FieldSpec("route", RISK_TABLE, CATEGORY, has_history=True),
    FieldSpec("risk_score", RISK_TABLE, NUMBER, bands=(20, 40, 60, 80), has_history=True),
    FieldSpec("fund_at_risk", RISK_TABLE, NUMBER, bands=(10_000, 100_000, 1_000_000)),
    FieldSpec("overdue_multiple", RISK_TABLE, NUMBER, bands=(1, 2, 3, 5), has_history=True),
    FieldSpec("pattern_is_reliable", RISK_TABLE, FLAG, has_history=True),
    FieldSpec("sig_heavy_withdrawal", RISK_TABLE, FLAG, has_history=True),
    FieldSpec("sig_dormant", RISK_TABLE, FLAG, has_history=True),
    FieldSpec("sig_broken_pattern", RISK_TABLE, FLAG, has_history=True),
    FieldSpec("sig_shrinking", RISK_TABLE, FLAG, has_history=True),
    FieldSpec("sig_going_dormant", RISK_TABLE, FLAG, has_history=True),
    FieldSpec("sig_never_repeated", RISK_TABLE, FLAG, has_history=True),
)

_FUND_FIELDS = (
    FieldSpec("balance", FUND_TABLE, NUMBER, bands=(10_000, 100_000, 1_000_000)),
    FieldSpec("months_until_empty", FUND_TABLE, NUMBER, bands=(3, 6, 12, 24)),
    FieldSpec("typical_gap_days", FUND_TABLE, NUMBER, bands=(30, 90, 180, 365)),
    FieldSpec("avg_deposit_amount", FUND_TABLE, NUMBER, bands=(10_000, 100_000, 1_000_000)),
    FieldSpec("last_deposit_amount", FUND_TABLE, NUMBER, bands=(10_000, 100_000, 1_000_000)),
    FieldSpec("deposit_trend", FUND_TABLE, NUMBER, bands=(-0.25, 0, 0.25)),
    FieldSpec("n_deposits", FUND_TABLE, NUMBER, bands=(1, 3, 5, 10)),
    FieldSpec("n_withdrawals", FUND_TABLE, NUMBER, bands=(1, 3, 5, 10)),
    FieldSpec("deposit_count_capped", FUND_TABLE, FLAG),
    FieldSpec("withdrawal_history_hidden", FUND_TABLE, FLAG),
)

FIELDS: dict[str, FieldSpec] = {spec.name: spec for spec in (*_RISK_FIELDS, *_FUND_FIELDS)}

FIELD_NAMES: tuple[str, ...] = tuple(FIELDS)

# The measures a tool may be asked for. Money is always rounded before it
# leaves; the average score is rounded to a whole number.
MEASURES: tuple[str, ...] = (
    "client_count",
    "fund_count",
    "money_total_kes",
    "money_at_risk_kes",
    "avg_risk_score",
)

# The measures that can be read from history, which holds risk numbers only.
HISTORY_MEASURES: tuple[str, ...] = (
    "client_count",
    "fund_count",
    "money_at_risk_kes",
    "avg_risk_score",
)

_MODELS = {RISK_TABLE: ClientRiskFeatures, FUND_TABLE: ActiveClientFund}


def field_spec(name: str) -> FieldSpec:
    """The spec for one field name, or a refusal naming the field."""
    spec = FIELDS.get(name)
    if spec is None:
        raise FilterRefused(f"'{name}' is not a field this agent may filter on")
    return spec


def check_measures(measures: Sequence[str], allowed: Sequence[str] = MEASURES) -> tuple[str, ...]:
    """The measures asked for, once every one of them is on the list."""
    if not measures:
        raise FilterRefused("ask for at least one measure")
    for measure in measures:
        if measure not in allowed:
            raise FilterRefused(f"'{measure}' is not a measure this agent may ask for")
    return tuple(measures)


def _column(spec: FieldSpec, *, history: bool):
    if history:
        if not spec.has_history:
            raise FilterRefused(f"'{spec.name}' is not kept in history, so it has no trend")
        return getattr(RiskSnapshot, spec.name)
    return getattr(_MODELS[spec.table], spec.name)


def _condition_parts(raw: Any) -> tuple[str, str, Any]:
    if not isinstance(raw, dict):
        raise FilterRefused("each condition must be an object with a field and an operator")
    field = raw.get("field")
    operator = raw.get("op")
    if not isinstance(field, str) or not isinstance(operator, str):
        raise FilterRefused("each condition needs a field name and an operator, both text")
    return field, operator, raw.get("value")


def _check_value(field: str, operator: str, value: Any) -> Any:
    """The value one condition carries, once it suits the operator."""
    settings = get_settings()
    if operator in VALUELESS_OPERATORS:
        return None
    if operator in LIST_OPERATORS:
        if not isinstance(value, list) or not value:
            raise FilterRefused(f"'{operator}' on '{field}' needs a non empty list of values")
        if len(value) > settings.agent_query_max_values:
            raise FilterRefused(
                f"'{operator}' on '{field}' lists more than "
                f"{settings.agent_query_max_values} values"
            )
        return value
    if value is None or isinstance(value, (list, dict)):
        raise FilterRefused(f"'{operator}' on '{field}' needs one plain value")
    return value


def _clause(column, operator: str, value: Any) -> ColumnElement[bool]:
    if operator == "eq":
        return column == value
    if operator == "ne":
        return column != value
    if operator == "gt":
        return column > value
    if operator == "gte":
        return column >= value
    if operator == "lt":
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "in":
        return column.in_(value)
    if operator == "not_in":
        return column.notin_(value)
    if operator == "is_null":
        return column.is_(None)
    if operator == "not_null":
        return column.isnot(None)
    if operator == "is_true":
        return column.is_(True)
    return column.is_(False)


def compile_conditions(
    conditions: Sequence[Any] | None, *, history: bool = False
) -> tuple[list[ColumnElement[bool]], list[dict[str, Any]]]:
    """Turn a list of conditions into query clauses and their tidy record.

    The second half of the pair is the filter exactly as it will be stored
    against a fact, so the number on the screen and the query behind it are
    written from the same thing.
    """
    conditions = list(conditions or [])
    settings = get_settings()
    if len(conditions) > settings.agent_query_max_conditions:
        raise FilterRefused(
            f"a filter may hold at most {settings.agent_query_max_conditions} conditions"
        )

    clauses: list[ColumnElement[bool]] = []
    recorded: list[dict[str, Any]] = []
    for raw in conditions:
        field, operator, value = _condition_parts(raw)
        spec = field_spec(field)
        if operator not in OPERATORS_BY_KIND[spec.kind]:
            allowed = ", ".join(OPERATORS_BY_KIND[spec.kind])
            raise FilterRefused(
                f"'{operator}' cannot be used on '{field}'. Allowed here: {allowed}"
            )
        checked = _check_value(field, operator, value)
        clauses.append(_clause(_column(spec, history=history), operator, checked))
        entry: dict[str, Any] = {"field": field, "op": operator}
        if checked is not None:
            entry["value"] = checked
        recorded.append(entry)
    return clauses, recorded


def touches_only_risk_fields(recorded: Sequence[dict[str, Any]]) -> None:
    """Refuse a filter that reaches outside the fields history keeps."""
    for entry in recorded:
        spec = field_spec(entry["field"])
        if not spec.has_history:
            raise FilterRefused(f"'{entry['field']}' is not kept in history, so it has no trend")
