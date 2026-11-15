"""Plain-English card copy for a proposal's situation, and a suggested owner for its response."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents import situations as sit
from app.agents.watchlist import GROUP_TO_SITUATION


@dataclass(frozen=True)
class SituationCopy:
    screen_label: str
    card_title: str
    why_now: str


class UnknownSituation(LookupError):
    pass


class UnknownResponseKind(LookupError):
    pass


SITUATION_COPY: dict[str, SituationCopy] = {
    sit.GRADUAL_DEPOSITOR_DUE: SituationCopy(
        screen_label="Overdue for a deposit",
        card_title="Set up a regular top-up with clients who have gone quiet",
        why_now="No recent purchase, and past their own usual gap",
    ),
    sit.GRADUAL_DEPOSITOR_WITHDRAWING: SituationCopy(
        screen_label="Taking money out, not putting any in",
        card_title="Review cash needs with clients who are withdrawing",
        why_now="Withdrawals with no purchase in recent months",
    ),
    sit.FREQUENT_DEPOSITOR_ONE_FUND: SituationCopy(
        screen_label="Regular depositors with one fund",
        card_title="Offer a second fund to regular depositors",
        why_now="Buying regularly, one fund held",
    ),
    sit.FREQUENT_DEPOSITOR_WITHDRAWING: SituationCopy(
        screen_label="Depositing and withdrawing",
        card_title="Show clients the benefit of staying invested",
        why_now="Withdrawals match or exceed deposits",
    ),
    sit.DEPOSIT_SIZE_FALLING: SituationCopy(
        screen_label="Contributions declining",
        card_title="Check in on clients whose contributions are shrinking",
        why_now="Contribution decline signal detected",
    ),
    sit.FIRST_DEPOSIT_NO_REPEAT: SituationCopy(
        screen_label="New clients needing a start",
        card_title="Welcome and encourage new clients to top up",
        why_now="First deposit signal detected",
    ),
    sit.FA_CALL_OVERDUE: SituationCopy(
        screen_label="Promised calls overdue",
        card_title="Follow up clients whose promised call has passed",
        why_now="A scheduled call is past its date",
    ),
    sit.RISK_ROSE_NO_CALL: SituationCopy(
        screen_label="Clients needing attention",
        card_title="Check in on clients whose risk rose with no response",
        why_now="Risk moved up, no response logged",
    ),
    sit.APPROACHING_PRESERVATION_LINE: SituationCopy(
        screen_label="Heading below the active line",
        card_title="Reach out to clients whose balance is heading below the active line",
        why_now="Balance on course to fall below the active line",
    ),
}


def _readable(group_name: str) -> str:
    return group_name.replace("_", " ").capitalize()


def copy_for(group_name: str) -> SituationCopy:
    code = GROUP_TO_SITUATION.get(group_name, group_name)
    try:
        return SITUATION_COPY[code]
    except KeyError:
        raise UnknownSituation(
            f"no card copy for situation '{code}' (from group_name '{group_name}')"
        ) from None


def copy_for_history(group_name: str) -> SituationCopy:
    """Card copy for a stored proposal; a group no longer produced reads as earlier."""
    try:
        return copy_for(group_name)
    except UnknownSituation:
        label = _readable(group_name)
        return SituationCopy(screen_label=label, card_title=label, why_now="Earlier suggestion")


RESPONSE_KIND_OWNER: dict[str, str] = {
    "automated_email": "System-sent",
    "campaign_enrolment": "System-sent",
    "product_teaching": "System-sent",
    "advisor_task": "Assigned FA",
    "phone_call": "Assigned FA",
    "monitor_only": "No one, recorded only",
    "escalate": "Assigned FA",
    "change_client_state": "System-sent",
    "ask_a_person_first": "Assigned FA",
}


def suggested_owner_for(response_kind: str | None) -> str | None:
    if response_kind is None:
        return None
    try:
        return RESPONSE_KIND_OWNER[response_kind]
    except KeyError:
        raise UnknownResponseKind(
            f"no suggested owner for response kind '{response_kind}'"
        ) from None


RESPONSE_KIND_LABEL: dict[str, str] = {
    "automated_email": "Email",
    "campaign_enrolment": "Campaign",
    "product_teaching": "Product guide",
    "advisor_task": "Advisor task",
    "phone_call": "Phone call",
    "monitor_only": "Monitor only",
    "escalate": "Escalate",
    "change_client_state": "Update client status",
    "ask_a_person_first": "Ask a person first",
}

NEXT_STEP_BY_RESPONSE_KIND: dict[str, str] = {
    "phone_call": "log_call",
    "automated_email": "client_record",
    "campaign_enrolment": "client_record",
}

NO_NEXT_STEP = "none"


def response_label_for(response_kind: str | None) -> str:
    return RESPONSE_KIND_LABEL.get(response_kind or "", "Suggestion")


def next_step_for(response_kind: str | None) -> str:
    return NEXT_STEP_BY_RESPONSE_KIND.get(response_kind or "", NO_NEXT_STEP)
