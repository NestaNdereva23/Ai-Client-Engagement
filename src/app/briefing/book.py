from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.routing.tier_playbook import SEGMENT_PLAYBOOK

_RULE = "-" * 78
DORMANT_SEGMENT = "active_dormant"


def format_money(value: float) -> str:
    return f"KSh {round(value):,}"


def compact_money(value: float) -> str:
    amount = round(value)
    if amount >= 1_000_000:
        return f"KSh {amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"KSh {amount / 1_000:.0f}K"
    return f"KSh {amount:,}"


def plural(count: int, word: str = "client") -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


@dataclass(frozen=True)
class TierLine:
    tier: str
    segment: str
    label: str
    goal: str | None
    clients: int
    balance_kes: float
    at_risk_kes: float


@dataclass(frozen=True)
class MonthEndPattern:
    usual_clients: int
    not_yet_clients: int

    @property
    def line(self) -> str:
        return (
            f"{plural(self.usual_clients)} usually top up in the last week of the month. "
            f"{self.not_yet_clients:,} {'has' if self.not_yet_clients == 1 else 'have'} "
            "not yet this month."
        )


@dataclass(frozen=True)
class BookFacts:
    advisor_name: str
    assigned_clients: int
    tiers: Sequence[TierLine]
    held_out_clients: int = 0
    not_yet_tiered_clients: int = 0
    month_end: MonthEndPattern | None = None
    computed_at: datetime | None = None

    @property
    def worklist_clients(self) -> int:
        return sum(line.clients for line in self.tiers)

    @property
    def worklist_balance_kes(self) -> float:
        return sum(line.balance_kes for line in self.tiers)

    @property
    def worklist_at_risk_kes(self) -> float:
        return sum(line.at_risk_kes for line in self.tiers)


def _tier_row(line: TierLine) -> str:
    return (
        f"  {line.label:<34}{plural(line.clients):>12}"
        f"   {format_money(line.balance_kes)} held"
        f"   {format_money(line.at_risk_kes)} at risk"
    )


def _segment_blocks(facts: BookFacts) -> list[str]:
    lines: list[str] = []
    for segment, playbook in SEGMENT_PLAYBOOK.items():
        tiers = [line for line in facts.tiers if line.segment == segment]
        if not tiers:
            continue
        lines.append("")
        lines.append(f"{playbook.label.upper()}  |  {playbook.goal}  ({playbook.definition})")
        lines.extend(_tier_row(line) for line in tiers)
    return lines


def _stands_out(facts: BookFacts) -> list[str]:
    notes: list[str] = []
    total_at_risk = facts.worklist_at_risk_kes
    if total_at_risk > 0:
        biggest = max(facts.tiers, key=lambda line: line.at_risk_kes)
        share = biggest.at_risk_kes / total_at_risk * 100
        notes.append(
            f"{biggest.label} hold the most at risk: {format_money(biggest.at_risk_kes)} "
            f"({share:,.0f}% of the total)."
        )
    dormant = sum(line.clients for line in facts.tiers if line.segment == DORMANT_SEGMENT)
    if dormant and facts.worklist_clients:
        playbook = SEGMENT_PLAYBOOK[DORMANT_SEGMENT]
        notes.append(
            f"{dormant:,} of {facts.worklist_clients:,} clients "
            f"({dormant / facts.worklist_clients * 100:,.0f}%) are in {playbook.label}: "
            f"{playbook.definition[0].lower()}{playbook.definition[1:]}."
        )
    if facts.month_end is not None:
        notes.append(facts.month_end.line)
    return notes


def _limits(facts: BookFacts) -> list[str]:
    notes = ["At risk is the balance times the risk score over 100. It is an estimate, not a loss."]
    if facts.held_out_clients:
        notes.append(
            f"Not on the worklist: {plural(facts.held_out_clients)} (no transactions in the "
            "review period, or below the capital preservation threshold)."
        )
    if facts.not_yet_tiered_clients:
        notes.append(
            f"No tier yet: {plural(facts.not_yet_tiered_clients)}. They are placed by the "
            "next nightly run."
        )
    return notes


def render_book_briefing(facts: BookFacts) -> str:
    lines = [
        f"BOOK BRIEFING  |  {facts.advisor_name}",
        f"{plural(facts.assigned_clients)} assigned   {facts.worklist_clients:,} on your worklist",
        _RULE,
        f"Holding      {format_money(facts.worklist_balance_kes)}",
        f"At risk      {format_money(facts.worklist_at_risk_kes)}",
    ]
    if facts.computed_at is not None:
        lines.append(f"Tiers as of  {facts.computed_at:%d %b %Y}")
    lines.extend(_segment_blocks(facts))

    lines.append("")
    lines.append("WHAT STANDS OUT IN YOUR BOOK")
    notes = _stands_out(facts)
    if notes:
        lines.extend(f"  - {note}" for note in notes)
    else:
        lines.append("  - Nothing on your worklist this morning.")

    lines.append("")
    lines.append("KEEP IN MIND - these figures have limits")
    lines.extend(f"  ! {note}" for note in _limits(facts))
    return "\n".join(lines)
