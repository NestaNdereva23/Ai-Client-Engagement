"""Turn tonight's watch list groups into proposed actions, using a fixed
rule table. No model is involved: every group maps to exactly one action,
and the whole point of this stage is to prove the shape works before any
model risk is taken on.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import action_is_paused, load_action
from app.agents.permissions import effective_permission
from app.agents.watchlist import (
    FEES_WILL_EMPTY,
    GETTING_SMALLER,
    HEALTHY_ONE_FUND,
    MORE_URGENT_BUT_NOT_CALLED,
    SIGNED_UP_RECENTLY,
    VERY_SMALL_AND_QUIET,
    WAITING_ON_A_CALL,
    GroupMember,
    WatchGroup,
    WatchlistThresholds,
    build_watchlist,
    load_thresholds,
)
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.active_clients import ActiveClientInteraction
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.complaints import ClientComplaint
from app.db.models.suppression import Suppression
from app.rules.catalog import angle_is_held

DO_NOTHING_ACTION = "do_nothing"

GROUP_ACTIONS: dict[str, str] = {
    SIGNED_UP_RECENTLY: "welcome_and_top_up",
    FEES_WILL_EMPTY: "fee_warning",
    VERY_SMALL_AND_QUIET: "start_win_back",
    GETTING_SMALLER: "ask_what_changed",
    HEALTHY_ONE_FUND: "suggest_second_fund",
    WAITING_ON_A_CALL: "follow_up_when_no_one_called",
    MORE_URGENT_BUT_NOT_CALLED: "check_in_rising_risk",
}

_CONTACT_TYPES = ("email_sent", "call_logged")

ON_DO_NOT_CONTACT_LIST = "on_do_not_contact_list"
OPEN_COMPLAINT = "open_complaint"
CONTACTED_RECENTLY = "contacted_recently"
ANGLE_PAUSED = "angle_paused"
ACTION_PAUSED = "action_paused"


class ProposalActionMissing(LookupError):
    """The rule table points at an action the live catalogue does not carry."""


def propose_watchlist(
    session: Session,
    as_of: date,
    *,
    run_id: str | None = None,
    thresholds: WatchlistThresholds | None = None,
    cooldown_days: int | None = None,
) -> list[AgentProposal]:
    """Run tonight's watch list and write one proposal per group that has
    someone in it. Groups with nobody in them tonight are skipped: there is
    no situation to record a decision about.
    """
    if thresholds is None:
        thresholds = load_thresholds(session, as_of)
    proposals = []
    for group in build_watchlist(session, as_of, thresholds):
        proposal = propose_group(
            session, group, thresholds, as_of, run_id=run_id, cooldown_days=cooldown_days
        )
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def propose_group(
    session: Session,
    group: WatchGroup,
    thresholds: WatchlistThresholds,
    as_of: date,
    *,
    run_id: str | None = None,
    cooldown_days: int | None = None,
) -> AgentProposal | None:
    """Write one proposal for one group, or None if the group is empty."""
    if not group.members:
        return None

    mapped_code = GROUP_ACTIONS.get(group.name)
    if mapped_code is None:
        raise ProposalActionMissing(f"the group '{group.name}' has no action in the rule table")
    mapped_action = _load_action_or_raise(session, mapped_code, as_of)

    skip_reasons = _skip_reasons(session, group.members, mapped_action, as_of, cooldown_days)
    included = [member for member in group.members if skip_reasons[_key(member)] is None]

    if included:
        action = mapped_action
    else:
        action = _load_action_or_raise(session, DO_NOTHING_ACTION, as_of)

    return _save_proposal(
        session,
        group=group,
        thresholds=thresholds,
        as_of=as_of,
        run_id=run_id,
        action=action,
        included=included,
        skip_reasons=skip_reasons,
    )


def _load_action_or_raise(session: Session, action_code: str, as_of: date) -> AgentActionCatalog:
    action = load_action(session, action_code, as_of)
    if action is None:
        raise ProposalActionMissing(
            f"'{action_code}' is not in the action catalogue in force on {as_of}"
        )
    return action


def _key(member: GroupMember) -> tuple[int, int]:
    return (member.client_id, member.unit_fund_id)


def _skip_reasons(
    session: Session,
    members: Sequence[GroupMember],
    action: AgentActionCatalog,
    as_of: date,
    cooldown_days: int | None,
) -> dict[tuple[int, int], str | None]:
    """Why each member of the group was left out, or None if they qualify."""
    if action_is_paused(session, action.action_code, as_of):
        return {_key(member): ACTION_PAUSED for member in members}

    settings = get_settings()
    cooldown = settings.agent_contact_cooldown_days if cooldown_days is None else cooldown_days
    return {
        _key(member): _member_skip_reason(session, member, action, as_of, cooldown)
        for member in members
    }


def _member_skip_reason(
    session: Session,
    member: GroupMember,
    action: AgentActionCatalog,
    as_of: date,
    cooldown_days: int,
) -> str | None:
    if session.get(Suppression, member.client_id) is not None:
        return ON_DO_NOT_CONTACT_LIST
    if _has_open_complaint(session, member.client_id):
        return OPEN_COMPLAINT
    if _contacted_recently(session, member, as_of, cooldown_days):
        return CONTACTED_RECENTLY
    if action.message_angle and angle_is_held(session, action.message_angle, as_of):
        return ANGLE_PAUSED
    return None


def _has_open_complaint(session: Session, client_id: int) -> bool:
    return (
        session.scalar(
            select(ClientComplaint.id)
            .where(ClientComplaint.client_id == client_id, ClientComplaint.status == "open")
            .limit(1)
        )
        is not None
    )


def _contacted_recently(
    session: Session, member: GroupMember, as_of: date, cooldown_days: int
) -> bool:
    cutoff = datetime.combine(as_of - timedelta(days=cooldown_days), datetime.min.time())
    return (
        session.scalar(
            select(ActiveClientInteraction.id)
            .where(
                ActiveClientInteraction.client_id == member.client_id,
                ActiveClientInteraction.unit_fund_id == member.unit_fund_id,
                ActiveClientInteraction.type.in_(_CONTACT_TYPES),
                ActiveClientInteraction.created_at >= cutoff,
            )
            .limit(1)
        )
        is not None
    )


def _save_proposal(
    session: Session,
    *,
    group: WatchGroup,
    thresholds: WatchlistThresholds,
    as_of: date,
    run_id: str | None,
    action: AgentActionCatalog,
    included: Sequence[GroupMember],
    skip_reasons: dict[tuple[int, int], str | None],
) -> AgentProposal:
    included_count = len({member.client_id for member in included})
    total_count = group.client_count
    money_total = sum(member.balance for member in included)

    proposal = AgentProposal(
        action_code=action.action_code,
        catalog_version=action.version,
        group_name=group.name,
        group_definition=dict(group.definition),
        client_count=total_count,
        money_total_kes=money_total,
        evidence=_evidence(group, thresholds),
        reason=_reason(action, group, included_count=included_count, total_count=total_count),
        angle=action.message_angle,
        content_mix=action.content_mix,
        permission_applied=effective_permission(session, action.action_code),
        status="proposed",
    )
    session.add(proposal)
    session.flush()

    session.add_all(
        AgentProposalClient(
            proposal_id=proposal.proposal_id,
            client_id=member.client_id,
            unit_fund_id=member.unit_fund_id,
            included=skip_reasons[_key(member)] is None,
            skip_reason=skip_reasons[_key(member)],
        )
        for member in group.members
    )
    session.flush()

    record_audit(
        session,
        entity_type="agent_proposal",
        action="create",
        entity_id=str(proposal.proposal_id),
        run_id=run_id,
        detail={
            "group_name": group.name,
            "action_code": action.action_code,
            "included_count": included_count,
            "excluded_count": total_count - included_count,
        },
    )
    return proposal


def _evidence(group: WatchGroup, thresholds: WatchlistThresholds) -> str:
    count = group.client_count
    if group.name == SIGNED_UP_RECENTLY:
        return (
            f"{count} clients made exactly one deposit, within the last "
            f"{thresholds.new_client_days} days."
        )
    if group.name == FEES_WILL_EMPTY:
        return (
            f"{count} clients have a balance above zero that the monthly fee will "
            f"empty in under {thresholds.months_until_empty:.1f} months."
        )
    if group.name == VERY_SMALL_AND_QUIET:
        return (
            f"{count} clients have a balance under {thresholds.small_balance:,.0f} "
            "and have stopped moving."
        )
    if group.name == GETTING_SMALLER:
        return f"{count} clients have deposits that are getting smaller over time."
    if group.name == HEALTHY_ONE_FUND:
        return f"{count} clients are in a healthy risk band and hold only one fund."
    if group.name == WAITING_ON_A_CALL:
        return (
            f"{count} clients have sat on the call list for more than "
            f"{thresholds.awaiting_call_days} days with nothing logged against them."
        )
    if group.name == MORE_URGENT_BUT_NOT_CALLED:
        return (
            f"{count} clients moved to a more urgent queue overnight but did not "
            "make this morning's call list."
        )
    return f"{count} clients matched the '{group.name}' group."


def _reason(
    action: AgentActionCatalog, group: WatchGroup, *, included_count: int, total_count: int
) -> str:
    if action.action_code == DO_NOTHING_ACTION:
        return (
            f"Every one of the {total_count} clients in '{group.name}' was left out "
            "by a check, so nothing is sent tonight."
        )
    left_out = total_count - included_count
    tail = f", {left_out} left out by the checks" if left_out else ""
    return f"{action.title} fits this group: {action.who}. {included_count} clients qualify{tail}."
