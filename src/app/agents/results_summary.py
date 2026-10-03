from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.action_performance import NONE_LABEL, ActionPerformance

PERCENT_DECIMALS = 1


@dataclass(frozen=True)
class ResultLine:
    action_code: str
    angle: str
    sent_count: int
    reply_percent: float
    opt_out_percent: float
    edit_percent: float
    deposit_percent: float
    money_in_kes: float
    periods: int


@dataclass(frozen=True)
class ResultsSummary:
    window_days: int
    period_hours: int
    lookback_hours: int
    min_group_size: int
    lines: tuple[ResultLine, ...]
    withheld_small_groups: int


def read_results(
    session: Session,
    *,
    action_code: str | None = None,
    angle: str | None = None,
    priority_tier: str | None = None,
    risk_band: str | None = None,
    content_mix: str | None = None,
    variant: str | None = None,
    window_days: int | None = None,
    now: datetime | None = None,
) -> ResultsSummary:
    settings = get_settings()
    period_hours = settings.action_performance_period_hours
    lookback_hours = settings.action_performance_lookback_hours
    window = window_days or settings.action_performance_read_window_days
    since = (now or datetime.now(UTC)) - timedelta(hours=lookback_hours)
    stmt = _summary_query(period_hours=period_hours, window_days=window, since=since)
    filters = {
        ActionPerformance.action_code: action_code,
        ActionPerformance.angle: angle,
        ActionPerformance.priority_tier: priority_tier,
        ActionPerformance.risk_band: risk_band,
        ActionPerformance.content_mix: content_mix,
        ActionPerformance.variant: variant,
    }
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(column == value)

    min_group_size = settings.agent_query_min_group_size
    rows = session.execute(stmt).all()
    return ResultsSummary(
        window_days=window,
        period_hours=period_hours,
        lookback_hours=lookback_hours,
        min_group_size=min_group_size,
        lines=tuple(_line_from(row) for row in rows if row.sent >= min_group_size),
        withheld_small_groups=sum(1 for row in rows if row.sent < min_group_size),
    )


def _summary_query(*, period_hours: int, window_days: int, since: datetime) -> Select[Any]:
    return (
        select(
            ActionPerformance.action_code,
            ActionPerformance.angle,
            func.sum(ActionPerformance.sent_count).label("sent"),
            func.sum(ActionPerformance.replied_count).label("replied"),
            func.sum(ActionPerformance.opted_out_count).label("opted_out"),
            func.sum(ActionPerformance.edited_count).label("edited"),
            func.sum(ActionPerformance.deposited_count).label("deposited"),
            func.sum(ActionPerformance.money_in_kes).label("money"),
            func.count(func.distinct(ActionPerformance.period_start)).label("periods"),
        )
        .where(
            ActionPerformance.period_hours == period_hours,
            ActionPerformance.window_days == window_days,
            ActionPerformance.period_start >= since,
        )
        .group_by(ActionPerformance.action_code, ActionPerformance.angle)
        .order_by(ActionPerformance.action_code, ActionPerformance.angle)
    )


def _line_from(row: Any) -> ResultLine:
    return ResultLine(
        action_code=row.action_code,
        angle=row.angle,
        sent_count=int(row.sent),
        reply_percent=_percent(row.replied, row.sent),
        opt_out_percent=_percent(row.opted_out, row.sent),
        edit_percent=_percent(row.edited, row.sent),
        deposit_percent=_percent(row.deposited, row.sent),
        money_in_kes=round(float(row.money)),
        periods=int(row.periods),
    )


def _percent(count: int, sent: int) -> float:
    return round(100 * int(count) / int(sent), PERCENT_DECIMALS)


def _line_as_dict(line: ResultLine) -> dict[str, Any]:
    return {
        "action_code": line.action_code,
        "angle": line.angle,
        "sent_count": line.sent_count,
        "reply_percent": line.reply_percent,
        "opt_out_percent": line.opt_out_percent,
        "edit_percent": line.edit_percent,
        "deposit_percent": line.deposit_percent,
        "money_in_kes": f"{line.money_in_kes:,.0f}",
        "periods": line.periods,
    }


def results_as_dict(summary: ResultsSummary) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "window_days": summary.window_days,
        "period_hours": summary.period_hours,
        "lookback_hours": summary.lookback_hours,
        "min_group_size": summary.min_group_size,
        "results": [_line_as_dict(line) for line in summary.lines],
        "withheld_small_groups": summary.withheld_small_groups,
    }
    if not summary.lines:
        answer["message"] = "nothing has been measured for this yet"
    return answer


def results_prompt_text(
    summary: ResultsSummary, *, ask: str, action_codes: Sequence[str] | None = None
) -> str:
    shown = [
        line for line in summary.lines if action_codes is None or line.action_code in action_codes
    ]
    body = [_prompt_line(line) for line in shown]
    if action_codes is not None:
        measured = {line.action_code for line in shown}
        body += [f"- {code}: nothing measured yet" for code in action_codes if code not in measured]
    if not body:
        body = ["Nothing has been measured yet."]
    header = (
        f"What earlier messages led to, looking back {_span(summary.lookback_hours)} and "
        f"measured {summary.window_days} days after each one was sent. "
        f"A line with fewer than {summary.min_group_size} messages is left out."
    )
    return "\n".join([header, *body, ask])


def _prompt_line(line: ResultLine) -> str:
    label = line.action_code
    if line.angle != NONE_LABEL:
        label = f"{label} with the angle {line.angle}"
    return (
        f"- {label}: {line.sent_count} sent, {line.reply_percent:g}% replied, "
        f"{line.opt_out_percent:g}% opted out, {line.edit_percent:g}% edited by a reviewer, "
        f"{line.deposit_percent:g}% deposited, {line.money_in_kes:,.0f} KES came in."
    )


def _span(hours: int) -> str:
    return f"{hours // 24} days" if hours % 24 == 0 else f"{hours} hours"
