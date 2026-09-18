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
    sit.NEW_CLIENT_SINGLE_DEPOSIT: SituationCopy(
        screen_label="New clients needing a start",
        card_title="Welcome and encourage new clients to top up",
        why_now="First deposit signal detected",
    ),
    sit.SYSTEM_FEE_PRESSURE: SituationCopy(
        screen_label="Balance under fee pressure",
        card_title="Prioritise clients who need a fee follow-up",
        why_now="Fee-related signal detected",
    ),
    sit.SMALL_BALANCE_INACTIVE: SituationCopy(
        screen_label="Small balances going quiet",
        card_title="Reconnect with clients who have gone quiet",
        why_now="Small balance and inactivity signal detected",
    ),
    sit.CONTRIBUTION_DECLINE: SituationCopy(
        screen_label="Contributions declining",
        card_title="Check in on clients whose contributions are shrinking",
        why_now="Contribution decline signal detected",
    ),
    sit.SINGLE_FUND_HEALTHY: SituationCopy(
        screen_label="Healthy clients with one fund",
        card_title="Offer a second fund to healthy single-fund clients",
        why_now="Healthy risk band, one fund held",
    ),
    sit.FOLLOW_UP_OVERDUE: SituationCopy(
        screen_label="Follow ups overdue",
        card_title="Follow up clients already due for a call",
        why_now="Call follow-up signal",
    ),
    sit.RISK_ACTION_GAP: SituationCopy(
        screen_label="Clients needing attention",
        card_title="Prioritise clients who moved to a more urgent queue overnight",
        why_now="Risk moved up, no call logged",
    ),
    sit.FEE_PRESSURE_GONE_QUIET: SituationCopy(
        screen_label="Fee pressure, gone quiet",
        card_title="Warn clients under fee pressure who have stopped depositing",
        why_now="Fee pressure and quiet deposits detected",
    ),
    sit.FEE_PRESSURE_ACTIVE_CONTRIBUTOR: SituationCopy(
        screen_label="Fee pressure, still contributing",
        card_title="Encourage clients under fee pressure who are still contributing",
        why_now="Fee pressure detected, deposits continuing",
    ),
}


def copy_for(group_name: str) -> SituationCopy:
    code = GROUP_TO_SITUATION.get(group_name, group_name)
    try:
        return SITUATION_COPY[code]
    except KeyError:
        raise UnknownSituation(
            f"no card copy for situation '{code}' (from group_name '{group_name}')"
        ) from None


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
