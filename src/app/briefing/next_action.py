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
    SITUATION_CODES,
)
from app.briefing.daily_brief import NO_GROUP_RANK, SITUATION_GROUP_RANK, relative_day

CALL = "call"
EMAIL = "email"

MEETING = "meeting"
NO_ANSWER = "no_answer"

CONTACT_VERBS = {
    "call_logged": "Called",
    "email_sent": "Emailed",
    "meeting_logged": "Met",
    "whatsapp_logged": "Messaged on WhatsApp",
}
PLANNED_LABELS = {"call": "Call", "meeting": "Meeting", "email": "Email", "reminder": "Reminder"}


@dataclass(frozen=True)
class OpenTouchpoint:
    id: int
    kind: str
    due_date: date


@dataclass(frozen=True)
class LoggedContact:
    type: str
    outcome: str | None
    day: date


@dataclass(frozen=True)
class NextActionFacts:
    situations: tuple[str, ...]
    planned: OpenTouchpoint | None
    missed: OpenTouchpoint | None
    last_contact: LoggedContact | None
    can_email: bool


@dataclass(frozen=True)
class NextAction:
    code: str
    title: str
    kind: str
    why: str
    email_purpose: str | None = None


@dataclass(frozen=True)
class Decision:
    action: NextAction | None
    note: str | None


@dataclass(frozen=True)
class SituationAction:
    title: str
    kind: str
    why: str
    email_purpose: str | None = None


SITUATION_ACTIONS: dict[str, SituationAction] = {
    RISK_ROSE_NO_CALL: SituationAction(
        "Reach out about the change",
        CALL,
        "Their risk went up and nobody has called.",
    ),
    FA_CALL_OVERDUE: SituationAction(
        "Follow up on the overdue call",
        CALL,
        "A call they were due has not happened.",
    ),
    GRADUAL_DEPOSITOR_WITHDRAWING: SituationAction(
        "Review their cash needs",
        CALL,
        "Money is going out while deposits slow. Help them split savings from spending.",
        "review_goals",
    ),
    FREQUENT_DEPOSITOR_WITHDRAWING: SituationAction(
        "Turn their activity into growth",
        CALL,
        "They deposit often but also take money out. Encourage net purchases and show "
        "the interest they have earned.",
        "grow_by_staying_invested",
    ),
    DEPOSIT_SIZE_FALLING: SituationAction(
        "Ask what changed",
        EMAIL,
        "Each deposit is smaller than before.",
        "ask_what_changed",
    ),
    GRADUAL_DEPOSITOR_DUE: SituationAction(
        "Review goals and set up a regular top up",
        CALL,
        "Their usual deposit is due. Agree a regular top up or standing order.",
    ),
    FIRST_DEPOSIT_NO_REPEAT: SituationAction(
        "Welcome them and ask for a second deposit",
        EMAIL,
        "They made one deposit and nothing since.",
        "welcome_and_top_up",
    ),
    FREQUENT_DEPOSITOR_ONE_FUND: SituationAction(
        "Suggest a second fund",
        CALL,
        "They deposit often and hold one fund. Introduce a second fund in a portfolio review.",
    ),
    APPROACHING_PRESERVATION_LINE: SituationAction(
        "Talk about topping up",
        CALL,
        "Their balance is close to the capital preservation line.",
        "encourage_top_up",
    ),
}

EMAIL_PURPOSES = {
    "grow_by_staying_invested": (
        "This client uses their investment actively. Thank them warmly for investing with us. "
        "Say you would like to help their money keep growing, and that the interest it "
        "earns builds up the longer it stays invested. Do not mention any deposit, "
        "withdrawal or money moving in or out. End by offering a short call to talk about "
        "their plans."
    ),
    "review_goals": (
        "This client's saving has slowed. Thank them warmly for investing with us and offer "
        "a short goals review, to help them plan what to save and what to keep for spending. "
        "Do not mention any deposit, withdrawal or money moving in or out. End by offering "
        "a short call."
    ),
    "encourage_top_up": (
        "This client saves regularly and all is well. Thank them warmly for saving with us "
        "and encourage them to keep adding to their investment. Keep the tone upbeat and "
        "calm. Never mention a balance, a line, a limit, a fee or any risk. Instead of "
        "offering a call, end with a gentle invitation to top up whenever they are ready, "
        "and say you are glad to help if they have questions."
    ),
    "reschedule_meeting": (
        "We missed a meeting that was planned. Say sorry we missed it and offer to find a new time."
    ),
    "ask_what_changed": (
        "Ask warmly whether anything has changed for them lately. Do not say that their "
        "deposits have changed."
    ),
    "welcome_and_top_up": (
        "They made their first deposit and nothing since. Welcome them warmly and ask "
        "whether they would like help with a second deposit."
    ),
}


def _lead_situation(situations: Sequence[str]) -> str | None:
    known = [code for code in situations if code in SITUATION_ACTIONS]
    if not known:
        return None
    return min(
        known,
        key=lambda code: (
            SITUATION_GROUP_RANK.get(code, NO_GROUP_RANK),
            SITUATION_CODES.index(code),
        ),
    )


def _upcoming(day: date, today: date) -> str:
    gap = (day - today).days
    if gap <= 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    return f"{day.day} {day:%B}"


def _planned_note(planned: OpenTouchpoint, today: date) -> str:
    return f"{PLANNED_LABELS[planned.kind]} planned for {_upcoming(planned.due_date, today)}"


def _contact_note(contact: LoggedContact, today: date) -> str:
    return f"{CONTACT_VERBS[contact.type]} {relative_day(contact.day, today)}"


def _missed_action(missed: OpenTouchpoint, today: date, can_email: bool) -> NextAction:
    when = relative_day(missed.due_date, today)
    if missed.kind == MEETING:
        return NextAction(
            f"missed_meeting:{missed.id}",
            "Follow up on missed meeting",
            EMAIL if can_email else CALL,
            f"The meeting due {when} is still open.",
            "reschedule_meeting" if can_email else None,
        )
    return NextAction(
        f"missed_call:{missed.id}",
        "Follow up on missed call",
        CALL,
        f"The call due {when} is still open.",
    )


def _try_again_action(contact: LoggedContact, today: date) -> NextAction:
    return NextAction(
        f"no_answer:{contact.day.isoformat()}",
        "Try them again",
        CALL,
        f"Your last attempt {relative_day(contact.day, today)} got no answer.",
    )


def _situation_action(situation: str, can_email: bool, why: str | None) -> NextAction:
    spec = SITUATION_ACTIONS[situation]
    kind = spec.kind if spec.kind == CALL or can_email else CALL
    purpose = spec.email_purpose if can_email else None
    return NextAction(situation, spec.title, kind, why or spec.why, purpose)


def _was_contacted_since(contact: LoggedContact | None, day: date) -> bool:
    return contact is not None and contact.day >= day


def decide(
    facts: NextActionFacts, today: date, quiet_days: int, situation_why: str | None = None
) -> Decision:
    contact = facts.last_contact
    if facts.planned is not None:
        return Decision(None, _planned_note(facts.planned, today))
    if facts.missed is not None and not _was_contacted_since(contact, facts.missed.due_date):
        return Decision(_missed_action(facts.missed, today, facts.can_email), None)
    if contact is not None and contact.outcome == NO_ANSWER and contact.day < today:
        return Decision(_try_again_action(contact, today), None)
    if contact is not None and (today - contact.day).days < quiet_days:
        return Decision(None, _contact_note(contact, today))
    situation = _lead_situation(facts.situations)
    if situation is None:
        return Decision(None, None)
    return Decision(_situation_action(situation, facts.can_email, situation_why), None)
