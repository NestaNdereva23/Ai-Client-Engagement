"""Deterministic rendering for the on-demand client briefing.

A direct port of the notebook's render_briefing: every line traces to a
field on BriefingFacts, nothing is inferred. If a fact is not on this
dataclass, this renderer cannot assert it -- the same discipline the
notebook itself calls out as the reason this ships before any model-assisted
version of the same page.

BriefingFacts carries no name: that is attached separately, after this
renders, since a name never needs to appear inside the fact block itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER

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
    days_since_purchase: int | None
    last_ticket: float | None
    own_rhythm_days: float | None
    overdue_multiple: float | None
    typical_ticket: float | None
    largest_ticket: float | None
    ticket_trend: float | None
    largest_real_redemption: float | None
    drawdown_depth: float | None
    days_since_real_redemption: int | None
    signals: dict[str, bool]
    purchases_censored: bool
    redemption_history_blind: bool
    holds_both_funds: bool
    fee_runway_months: float | None
    fee_runway_threshold: float
    has_open_complaint: bool


def render_briefing(facts: BriefingFacts) -> str:
    """The plain-text briefing page, matching the notebook's own layout."""
    lines: list[str] = [
        f"CLIENT BRIEFING  |  {facts.client_code}  |  {facts.fund_name}",
        f"Risk {facts.risk_score}/100 ({facts.risk_band})   Route: {facts.route}",
        _RULE,
        f"Holding      KES {facts.balance:,.2f}  ({facts.balance_tier})",
    ]

    if facts.days_since_purchase is not None:
        line = f"Last deposit {facts.days_since_purchase:,.0f} days ago"
        if facts.last_ticket is not None:
            line += f", KES {facts.last_ticket:,.0f}"
        lines.append(line)

    if facts.own_rhythm_days is not None and facts.own_rhythm_days >= 1:
        rhythm_line = f"Their pattern was roughly every {facts.own_rhythm_days:,.0f} days"
        if facts.overdue_multiple is not None and facts.overdue_multiple > 1:
            rhythm_line += f" - now {facts.overdue_multiple:,.1f}x overdue"
        lines.append(rhythm_line)
    else:
        lines.append("Their pattern  not measurable from the returned purchases")

    if facts.typical_ticket is not None:
        largest = facts.largest_ticket if facts.largest_ticket is not None else 0.0
        lines.append(
            f"Typical top-up  KES {facts.typical_ticket:,.0f}   largest KES {largest:,.0f}"
        )

    if facts.ticket_trend is not None:
        direction = "shrinking" if facts.ticket_trend < 0 else "growing"
        lines.append(f"Deposit trend   {direction} ({facts.ticket_trend:+.2f} log10 per top-up)")

    if facts.largest_real_redemption is not None:
        depth = facts.drawdown_depth if facts.drawdown_depth is not None else 0.0
        days_ago = facts.days_since_real_redemption
        days_ago = days_ago if days_ago is not None else 0
        lines.append(
            f"Largest visible redemption  KES {facts.largest_real_redemption:,.0f}"
            f"  ({depth * 100:,.0f}% of the balance it left)"
            f"  {days_ago:,.0f} days ago"
        )
    else:
        lines.append("Redemptions     none visible in the returned window (see caveats)")

    lines.append("")
    lines.append("WHY THIS CLIENT SURFACED")
    for name in SIGNAL_ORDER:
        if facts.signals.get(name):
            lines.append(f"  - {SIGNAL_LABELS[name]}")

    caveats: list[str] = []
    if facts.purchases_censored:
        caveats.append("purchase history truncated at 5 - true frequency is unknown")
    if facts.redemption_history_blind:
        caveats.append("both sale slots hold system postings - redemption history is hidden")
    if facts.holds_both_funds:
        caveats.append("this client also holds a position in the other fund")
    if facts.fee_runway_months is not None and facts.fee_runway_months < facts.fee_runway_threshold:
        caveats.append(f"balance covers only {facts.fee_runway_months:,.1f} months of fees")
    if facts.has_open_complaint:
        caveats.append("this client has an open complaint")

    if caveats:
        lines.append("")
        lines.append("CAVEATS - do not assert beyond these")
        lines.extend(f"  ! {c}" for c in caveats)

    return "\n".join(lines)
