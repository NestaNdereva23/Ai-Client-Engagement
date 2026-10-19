from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.agents.situations import (
    APPROACHING_PRESERVATION_LINE,
    DEPOSIT_SIZE_FALLING,
    FA_CALL_OVERDUE,
    FIRST_DEPOSIT_NO_REPEAT,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    RISK_ROSE_NO_CALL,
)
from app.briefing.book import MonthEndPattern, compact_money, format_money
from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER
from app.routing.sides import CAPITAL_PRESERVATION_KES

NO_GROUP_RANK = 99
WITHDRAWING = "withdrawing"
CALLS = "calls"
STOPPED = "stopped"
NEAR_LINE = "near_line"
PAYING_LESS = "paying_less"
SECOND_PRODUCT = "second_product"


@dataclass(frozen=True)
class GroupSpec:
    code: str
    situations: tuple[str, ...]
    title: str
    happening: str
    todo: str
    unknown: str
    reason: str


BRIEF_GROUPS: tuple[GroupSpec, ...] = (
    GroupSpec(
        WITHDRAWING,
        (GRADUAL_DEPOSITOR_WITHDRAWING, FREQUENT_DEPOSITOR_WITHDRAWING),
        "Clients taking money out while paying in less",
        "These clients have withdrawn money recently, and their deposits are getting smaller "
        "or coming less often.",
        "Call them first. Ask whether their cash needs have changed, and offer to split their "
        "money between savings and spending.",
        "We can see what changed, not why. A withdrawal can be planned. Only the client can "
        "tell you.",
        "Taking money out while deposits slow down.",
    ),
    GroupSpec(
        CALLS,
        (FA_CALL_OVERDUE, RISK_ROSE_NO_CALL),
        "Calls to catch up on",
        "A planned call is overdue, or a client's situation got worse and nobody has called.",
        "Make or rebook these calls today.",
        "A call may have happened that was not logged.",
        "A call is overdue, or their situation changed and nobody has called.",
    ),
    GroupSpec(
        STOPPED,
        (GRADUAL_DEPOSITOR_DUE, FIRST_DEPOSIT_NO_REPEAT),
        "Clients who have stopped investing",
        "These clients have gone quiet. Many used to deposit on a regular rhythm, and the "
        "longer they stay quiet, the harder they are to win back.",
        "Send a short, friendly check-in to those who used to deposit regularly. Call the "
        "larger balances yourself.",
        "We do not know if they changed plans, moved money elsewhere, or simply forgot.",
        "Has gone quiet.",
    ),
    GroupSpec(
        NEAR_LINE,
        (APPROACHING_PRESERVATION_LINE,),
        "Balances close to the capital preservation line",
        "These balances are getting close to the line below which a client leaves your worklist.",
        "Talk to them about topping up before they cross it.",
        "A small balance may be intentional.",
        "Balance is close to the capital preservation line.",
    ),
    GroupSpec(
        PAYING_LESS,
        (DEPOSIT_SIZE_FALLING,),
        "Clients paying in less",
        "These clients still deposit, but each deposit is smaller than before.",
        "Ask whether their income or goals have changed, and agree a deposit that suits them.",
        "Smaller deposits can be seasonal.",
        "Deposits are getting smaller.",
    ),
    GroupSpec(
        SECOND_PRODUCT,
        (FREQUENT_DEPOSITOR_ONE_FUND,),
        "Clients ready for a second product",
        "These clients deposit often and hold a single fund.",
        "Introduce a second product in a portfolio review.",
        "We do not know their appetite for another product.",
        "Deposits often and holds one fund.",
    ),
)

GROUP_BY_CODE = {spec.code: spec for spec in BRIEF_GROUPS}
CLIENT_SPECIFIC_GROUPS = frozenset({WITHDRAWING, STOPPED, NEAR_LINE, PAYING_LESS})
GROUP_CODES = tuple(spec.code for spec in BRIEF_GROUPS)
SITUATION_GROUP_RANK = {
    situation: rank for rank, spec in enumerate(BRIEF_GROUPS) for situation in spec.situations
}

CALENDAR_LABELS = {
    "call": "Call",
    "meeting": "Meeting",
    "email": "Email",
    "reminder": "Reminder",
    "whatsapp": "WhatsApp follow-up",
}
CALENDAR_PHRASES = {
    "call": "a call",
    "meeting": "a meeting",
    "email": "an email to send",
    "reminder": "a reminder",
    "whatsapp": "a WhatsApp follow-up",
}
TIMED_KINDS = ("call", "meeting")
PHRASES_SHOWN = 3
URGENT_HEADLINE_KINDS = ("large_withdrawals", "overdue_calls")

BRIEF_SIGNAL_WORDS = {
    **{name: label.lower() for name, label in SIGNAL_LABELS.items()},
    "sig_broken_pattern": "activity changed from usual",
    "sig_heavy_withdrawal": "large withdrawal",
    "sig_shrinking": "deposits getting smaller",
}


@dataclass(frozen=True)
class Headline:
    kind: str
    lead: str
    text: str
    short: str
    count: int
    amount_kes: float | None = None


@dataclass(frozen=True)
class BriefClient:
    client_id: int
    unit_fund_id: int
    client_code: str | None
    balance_kes: float | None
    fund_at_risk_kes: float | None
    reason: str


@dataclass(frozen=True)
class CalendarItem:
    client_id: int
    kind: str
    label: str
    time: str | None


@dataclass(frozen=True)
class BriefGroup:
    code: str
    title: str
    clients: int
    could_leave_kes: float
    share: float
    what_is_happening: str
    what_to_do: str
    worth_knowing: str
    sharp_fact: str | None
    examples: tuple[BriefClient, ...]
    warning_signs: str | None = None


@dataclass(frozen=True)
class DailyBrief:
    fa_id: str
    advisor_name: str
    as_of: date
    assigned_clients: int
    worklist_clients: int
    at_risk_kes: float
    headlines: tuple[Headline, ...]
    calendar: tuple[CalendarItem, ...]
    groups: tuple[BriefGroup, ...]
    rest_clients: int
    rest_could_leave_kes: float
    month_end: MonthEndPattern | None = None

    @property
    def subject(self) -> str:
        if self.worklist_clients == 0:
            return "Nothing on your worklist today"
        parts = []
        urgent = next((h for h in self.headlines if h.kind in URGENT_HEADLINE_KINDS), None)
        if urgent is not None:
            parts.append(urgent.short)
        to_review = sum(group.clients for group in self.groups)
        if to_review:
            parts.append(f"{to_review:,} {plural(to_review, 'client')} to review today")
        if not parts:
            return "Nothing new since yesterday"
        text = ", ".join(parts)
        return text[0].upper() + text[1:]


def group_code_for_rank(rank: int | None) -> str | None:
    if rank is None or rank >= len(BRIEF_GROUPS):
        return None
    return BRIEF_GROUPS[rank].code


def plural(count: int, singular: str, many: str | None = None) -> str:
    return singular if count == 1 else (many or f"{singular}s")


def relative_day(day: date, today: date) -> str:
    gap = (today - day).days
    if gap <= 0:
        return "today"
    if gap == 1:
        return "yesterday"
    if gap < 14:
        return f"{gap} days ago"
    return f"on {day.day} {day:%B}"


def time_span(days: int) -> str:
    if days < 14:
        return f"{days} days"
    if days < 60:
        return f"{round(days / 7)} weeks"
    return f"{round(days / 30)} months"


def rhythm_phrase(gap_days: float) -> str:
    if gap_days <= 10:
        return "about every week"
    if gap_days <= 20:
        return "about every two weeks"
    if gap_days <= 40:
        return "about every month"
    return f"about every {round(gap_days / 30)} months"


def join_with_and(parts: Sequence[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def large_withdrawals_headline(count: int, amount: float, when: str) -> Headline:
    who = (
        "1 client made a large withdrawal"
        if count == 1
        else f"{count:,} clients made large withdrawals"
    )
    short = f"{count:,} large {plural(count, 'withdrawal')} {when}"
    return Headline(
        "large_withdrawals",
        f"{who} {when}.",
        f"{compact_money(amount)} in total.",
        short,
        count,
        amount,
    )


def _calendar_phrase(item: CalendarItem) -> str:
    phrase = CALENDAR_PHRASES[item.kind]
    if item.time and item.kind in TIMED_KINDS:
        return f"{phrase} at {item.time}"
    return phrase


def calendar_headline(items: Sequence[CalendarItem]) -> Headline:
    count = len(items)
    shown = [_calendar_phrase(item) for item in items[:PHRASES_SHOWN]]
    if count == 1:
        return Headline(
            "calendar", f"You have {shown[0]} today.", "", "1 thing on your calendar", 1
        )
    if count > PHRASES_SHOWN:
        shown.append(f"{count - PHRASES_SHOWN} more")
    text = join_with_and(shown)
    return Headline(
        "calendar",
        f"You have {count:,} {plural(count, 'thing')} on your calendar today.",
        f"{text[0].upper()}{text[1:]}.",
        f"{count:,} {plural(count, 'thing')} on your calendar",
        count,
    )


def below_line_headline(count: int) -> Headline:
    lead = (
        "1 client's balance fell below the capital preservation line."
        if count == 1
        else f"{count:,} clients' balances fell below the capital preservation line."
    )
    return Headline(
        "below_line",
        lead,
        "They have left your worklist.",
        f"{count:,} {plural(count, 'balance')} below the line",
        count,
    )


def overdue_calls_headline(count: int) -> Headline:
    verb = "is" if count == 1 else "are"
    return Headline(
        "overdue_calls",
        f"{count:,} planned {plural(count, 'call')} {verb} overdue.",
        "",
        f"{count:,} overdue {plural(count, 'call')}",
        count,
    )


def deposits_headline(count: int, amount: float, when: str) -> Headline:
    return Headline(
        "deposits",
        "Good news:",
        f"{count:,} {plural(count, 'client')} made a deposit {when}, "
        f"{compact_money(amount)} in total.",
        f"{count:,} {plural(count, 'client')} topped up",
        count,
        amount,
    )


def half_balance_fact(count: int, amount: float, recent: int, when: str) -> str | None:
    if count == 0:
        return None
    who = "1 of them has" if count == 1 else f"{count:,} of them have"
    text = (
        f"{who} taken out more than half of their balance in the last 30 days, "
        f"{compact_money(amount)} in total."
    )
    if recent:
        text += f" {recent:,} of them did it {when}."
    return text


def long_quiet_fact(count: int) -> str | None:
    if count == 0:
        return None
    who = "1 of them has" if count == 1 else f"{count:,} of them have"
    return f"{who} not made a deposit for over six months."


def withdrawal_reason(
    heavy: bool,
    amount: float | None,
    balance: float | None,
    last_withdrawal: date | None,
    today: date,
) -> str:
    ending = (
        f" Last withdrawal {relative_day(last_withdrawal, today)}."
        if last_withdrawal is not None
        else ""
    )
    if heavy and amount is not None and balance is not None and balance + amount > 0:
        share = round(amount / (balance + amount) * 100)
        return f"Took out {compact_money(amount)}, {share}% of the balance, at once.{ending}"
    return f"Has taken out money while deposits slowed.{ending}"


def quiet_reason(
    deposits: int, last_deposit: date | None, typical_gap_days: float | None, today: date
) -> str:
    if deposits == 1:
        return "Made one deposit and none since."
    if last_deposit is None:
        return GROUP_BY_CODE[STOPPED].reason
    ago = f"Last deposit was {time_span((today - last_deposit).days)} ago."
    if typical_gap_days:
        return f"Used to deposit {rhythm_phrase(typical_gap_days)}. {ago}"
    return ago


def near_line_reason(balance: float | None) -> str:
    if balance is None:
        return GROUP_BY_CODE[NEAR_LINE].reason
    line = format_money(CAPITAL_PRESERVATION_KES)
    return f"Balance is {format_money(balance)}, close to the {line} line."


def paying_less_reason(last_deposit_amount: float | None) -> str:
    if last_deposit_amount is None:
        return GROUP_BY_CODE[PAYING_LESS].reason
    return f"Deposits are getting smaller. The last one was {compact_money(last_deposit_amount)}."


def warning_signs_fact(counts: dict[str, int], clients: int) -> str | None:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], SIGNAL_ORDER.index(item[0])))
    if not ranked:
        return None
    if clients == 1:
        names = [BRIEF_SIGNAL_WORDS[name] for name, _ in ranked[:3]]
        return f"Warning signs: {_joined(names)}."
    shown = [f"{BRIEF_SIGNAL_WORDS[name]} ({count} of {clients})" for name, count in ranked[:2]]
    return f"Most common warning signs: {', '.join(shown)}."


def _joined(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def client_reason(code: str, row, today: date) -> str:
    if code == WITHDRAWING:
        return withdrawal_reason(
            bool(row.sig_heavy_withdrawal),
            row.largest_withdrawal,
            row.balance,
            row.last_withdrawal_date,
            today,
        )
    if code == STOPPED:
        return quiet_reason(row.n_deposits, row.last_deposit_date, row.typical_gap_days, today)
    if code == NEAR_LINE:
        return near_line_reason(row.balance)
    if code == PAYING_LESS:
        return paying_less_reason(row.last_deposit_amount)
    return GROUP_BY_CODE[code].reason
