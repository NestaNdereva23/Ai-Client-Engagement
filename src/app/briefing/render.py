"""Deterministic rendering for the on-demand client briefing.

Started as a direct port of the notebook's render_briefing; the wording has
since been reworked for an FA audience (plain percentages instead of raw
log10 slopes, no unexplained jargon), so it's no longer a byte-for-byte
match to the notebook's own output -- see test_briefing_render.py. What the
port kept is the discipline: every line traces to a field on BriefingFacts,
nothing is inferred. If a fact is not on this dataclass, this renderer
cannot assert it -- the same discipline the notebook itself calls out as
the reason this ships before any model-assisted version of the same page.

BriefingFacts carries no name: that is attached separately, after this
renders, since a name never needs to appear inside the fact block itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER
from app.transform.features import TREND_EPS

_RULE = "-" * 78


@dataclass(frozen=True)
class BriefingFacts:
    """Everything render_briefing needs for one client-fund relationship.

    The score, band, route and signals come from client_risk_features; the
    behavioural detail comes from active_client_fund; has_open_complaint is
    the one addition beyond the notebook's own fact set.
    """

    client_code: str | None
    fund_name: str
    risk_score: int
    risk_band: str
    route: str | None
    balance: float
    balance_tier: str
    days_since_deposit: int | None
    last_deposit_amount: float | None
    typical_gap_days: float | None
    overdue_multiple: float | None
    typical_deposit_amount: float | None
    largest_deposit_amount: float | None
    deposit_trend: float | None
    largest_withdrawal: float | None
    withdrawal_pct: float | None
    days_since_withdrawal: int | None
    signals: dict[str, bool]
    deposit_count_capped: bool
    withdrawal_history_hidden: bool
    holds_both_funds: bool
    months_until_empty: float | None
    months_until_empty_threshold: float
    has_open_complaint: bool
    # Not read by render_briefing itself -- carried here only so
    # services/briefing.py's narrative path (AM15) reads the same
    # already-gathered row this dataclass represents, rather than a second
    # query that could disagree with it. None when the nightly job hasn't
    # bucketed this client-fund yet.
    recency_band: str | None = None
    value_tier: str | None = None


def render_briefing(facts: BriefingFacts) -> str:
    """The plain-text briefing page, matching the notebook's own layout."""
    lines: list[str] = [
        f"CLIENT BRIEFING  |  {facts.client_code}  |  {facts.fund_name}",
        f"Risk {facts.risk_score}/100 ({facts.risk_band})   Route: {facts.route}",
        _RULE,
        f"Holding      KES {facts.balance:,.2f}  ({facts.balance_tier})",
    ]

    if facts.days_since_deposit is not None:
        line = f"Last deposit {facts.days_since_deposit:,.0f} days ago"
        if facts.last_deposit_amount is not None:
            line += f", KES {facts.last_deposit_amount:,.0f}"
        lines.append(line)

    if facts.typical_gap_days is not None and facts.typical_gap_days >= 1:
        pattern_line = f"Their pattern was roughly every {facts.typical_gap_days:,.0f} days"
        if facts.overdue_multiple is not None and facts.overdue_multiple > 1:
            pattern_line += (
                f" - it's now been {facts.overdue_multiple:,.1f}x that long (well overdue)"
            )
        lines.append(pattern_line)
    else:
        lines.append("Their pattern  not measurable from the returned deposits")

    if facts.typical_deposit_amount is not None:
        largest = facts.largest_deposit_amount if facts.largest_deposit_amount is not None else 0.0
        lines.append(
            f"Typical top-up  KES {facts.typical_deposit_amount:,.0f}   largest KES {largest:,.0f}"
        )

    if facts.deposit_trend is not None:
        if abs(facts.deposit_trend) < TREND_EPS:
            lines.append("Deposit trend   holding steady - no clear rise or fall in top-up size")
        elif facts.deposit_trend < 0:
            pct = (1 - 10**facts.deposit_trend) * 100
            lines.append(f"Deposit trend   shrinking - about {pct:,.0f}% less each top-up")
        else:
            pct = (10**facts.deposit_trend - 1) * 100
            lines.append(f"Deposit trend   growing - about {pct:,.0f}% more each top-up")

    if facts.largest_withdrawal is not None:
        pct = facts.withdrawal_pct if facts.withdrawal_pct is not None else 0.0
        days_ago = facts.days_since_withdrawal
        days_ago = days_ago if days_ago is not None else 0
        lines.append(
            f"Largest visible withdrawal  KES {facts.largest_withdrawal:,.0f}"
            f"  ({pct * 100:,.0f}% of the balance it left)"
            f"  {days_ago:,.0f} days ago"
        )
    else:
        lines.append("Withdrawals     none visible in the returned window (see note below)")

    lines.append("")
    lines.append("WHY THIS CLIENT SURFACED")
    for name in SIGNAL_ORDER:
        if facts.signals.get(name):
            lines.append(f"  - {SIGNAL_LABELS[name]}")

    caveats: list[str] = []
    if facts.deposit_count_capped:
        caveats.append(
            "only their last 5 deposits are visible here - their true frequency may differ"
        )
    if facts.withdrawal_history_hidden:
        caveats.append(
            "we can't see this client's withdrawal history - both withdrawal records"
            " are internal system entries, not real transactions"
        )
    if facts.holds_both_funds:
        caveats.append("this client also holds a position in the other fund")
    if (
        facts.months_until_empty is not None
        and facts.months_until_empty < facts.months_until_empty_threshold
    ):
        caveats.append(f"balance covers only {facts.months_until_empty:,.1f} months of fees")
    if facts.has_open_complaint:
        caveats.append("this client has an open complaint")

    if caveats:
        lines.append("")
        lines.append("KEEP IN MIND - don't say more to the client than these allow")
        lines.extend(f"  ! {c}" for c in caveats)

    return "\n".join(lines)
