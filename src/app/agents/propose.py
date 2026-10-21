"""Turn tonight's watch list groups into proposed actions with a fixed rule table.
No model is involved: every group maps to exactly one action."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import action_is_paused, load_action
from app.agents.action_rules import already_offered, lacks_an_approved_guide
from app.agents.fa_scope import current_fa_id
from app.agents.permissions import effective_permission
from app.agents.situation_action_mapping import action_code_for_situation
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
from app.agents.watchlist import (
    GROUP_TO_SITUATION,
    GroupMember,
    WatchGroup,
    WatchlistThresholds,
    build_watchlist,
    load_thresholds,
)
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.active_clients import (
    CONTACT_INTERACTION_TYPES,
    ActiveClientFund,
    ActiveClientInteraction,
)
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.complaints import ClientComplaint
from app.db.models.suppression import Suppression
from app.rules.catalog import angle_is_held

DO_NOTHING_ACTION = "do_nothing"
WATCH_FOR_NOW_ACTION = "watch_for_now"

GROUP_ACTIONS: dict[str, str] = {
    GRADUAL_DEPOSITOR_DUE: "give_advisor_a_task",
    GRADUAL_DEPOSITOR_WITHDRAWING: "give_advisor_a_task",
    FREQUENT_DEPOSITOR_ONE_FUND: "suggest_second_fund",
    FREQUENT_DEPOSITOR_WITHDRAWING: "send_learning_note",
    DEPOSIT_SIZE_FALLING: "ask_what_changed",
    FIRST_DEPOSIT_NO_REPEAT: "welcome_and_top_up",
    FA_CALL_OVERDUE: "follow_up_when_no_one_called",
    RISK_ROSE_NO_CALL: "check_in_rising_risk",
    APPROACHING_PRESERVATION_LINE: "give_advisor_a_task",
}

ON_DO_NOT_CONTACT_LIST = "on_do_not_contact_list"
OPEN_COMPLAINT = "open_complaint"
CONTACTED_RECENTLY = "contacted_recently"
ANGLE_PAUSED = "angle_paused"
ACTION_PAUSED = "action_paused"
NO_APPROVED_GUIDE = "no_approved_guide"
ALREADY_OFFERED = "already_offered"
CONSOLIDATED_INTO_ANOTHER_SITUATION = "consolidated_into_another_situation"

SITUATION_PRIORITY: tuple[str, ...] = (
    RISK_ROSE_NO_CALL,
    FA_CALL_OVERDUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    DEPOSIT_SIZE_FALLING,
    GRADUAL_DEPOSITOR_DUE,
    FIRST_DEPOSIT_NO_REPEAT,
    FREQUENT_DEPOSITOR_ONE_FUND,
    APPROACHING_PRESERVATION_LINE,
)


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
    """Run tonight's watch list and write one proposal per group that has someone in it.
    Groups with nobody in them are skipped."""
    if thresholds is None:
        thresholds = load_thresholds(session, as_of)
    groups = build_watchlist(session, as_of, thresholds)
    winners = situation_winners(groups)
    proposals = []
    for group in groups:
        proposal = propose_group(
            session,
            group,
            thresholds,
            as_of,
            run_id=run_id,
            cooldown_days=cooldown_days,
            winners=winners,
        )
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def situation_winners(groups: Sequence[WatchGroup]) -> dict[tuple[int, int], str]:
    """Which one situation wins for each client fund carrying more than one.
    A fund in several groups gets one action, the most urgent situation's."""
    by_name = {group.name: group for group in groups}
    winners: dict[tuple[int, int], str] = {}
    for group_name in SITUATION_PRIORITY:
        group = by_name.get(group_name)
        if group is None:
            continue
        for member in group.members:
            winners.setdefault(member_key(member), group_name)
    return winners


def default_action_code_for_group(session: Session, group_name: str, as_of: date) -> str | None:
    """The action a group falls back to when nothing else picks one for it.
    The mapping table comes first; GROUP_ACTIONS is the fallback when none is published."""
    situation_code = GROUP_TO_SITUATION.get(group_name, group_name)
    return action_code_for_situation(session, situation_code, as_of) or GROUP_ACTIONS.get(
        group_name
    )


def propose_group(
    session: Session,
    group: WatchGroup,
    thresholds: WatchlistThresholds,
    as_of: date,
    *,
    run_id: str | None = None,
    cooldown_days: int | None = None,
    winners: dict[tuple[int, int], str] | None = None,
) -> AgentProposal | None:
    """Write one proposal for one group, or None if the group is empty."""
    if not group.members:
        return None

    mapped_code = default_action_code_for_group(session, group.name, as_of)
    if mapped_code is None:
        raise ProposalActionMissing(f"the group '{group.name}' has no action in the rule table")
    mapped_action = load_action_or_raise(session, mapped_code, as_of)

    skip_reasons = group_skip_reasons(session, group.members, mapped_action, as_of, cooldown_days)
    if winners is not None:
        for member in group.members:
            key = member_key(member)
            if winners.get(key) != group.name:
                skip_reasons[key] = CONSOLIDATED_INTO_ANOTHER_SITUATION
    included = [member for member in group.members if skip_reasons[member_key(member)] is None]

    if included:
        action = mapped_action
    else:
        action = load_action_or_raise(session, DO_NOTHING_ACTION, as_of)

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


def load_action_or_raise(session: Session, action_code: str, as_of: date) -> AgentActionCatalog:
    """One action from the live catalogue, or raise ProposalActionMissing.
    Shared with the model driven flow, so a run fails loudly on a missing action."""
    action = load_action(session, action_code, as_of)
    if action is None:
        raise ProposalActionMissing(
            f"'{action_code}' is not in the action catalogue in force on {as_of}"
        )
    return action


def member_key(member: GroupMember) -> tuple[int, int]:
    """A client fund's identity within a group, for keying a skip reason to it."""
    return (member.client_id, member.unit_fund_id)


def skip_reason_counts(skip_reasons: dict[tuple[int, int], str | None]) -> dict[str, int]:
    """How many client funds were left out, tallied by reason.
    Included members do not appear."""
    counts: dict[str, int] = {}
    for reason in skip_reasons.values():
        if reason is None:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def group_skip_reasons(
    session: Session,
    members: Sequence[GroupMember],
    action: AgentActionCatalog,
    as_of: date,
    cooldown_days: int | None,
) -> dict[tuple[int, int], str | None]:
    """Why each member of the group was left out, or None if they qualify.
    Shared with the model driven flow, so both run the same gates."""
    if action_is_paused(session, action.action_code, as_of):
        return {member_key(member): ACTION_PAUSED for member in members}
    if lacks_an_approved_guide(session, action):
        return {member_key(member): NO_APPROVED_GUIDE for member in members}

    settings = get_settings()
    cooldown = settings.agent_contact_cooldown_days if cooldown_days is None else cooldown_days
    return {
        member_key(member): member_skip_reason(session, member, action, as_of, cooldown)
        for member in members
    }


def member_skip_reason(
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
    if already_offered(session, member.client_id, action):
        return ALREADY_OFFERED
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
                ActiveClientInteraction.type.in_(CONTACT_INTERACTION_TYPES),
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
        fa_id=current_fa_id(),
        action_code=action.action_code,
        catalog_version=action.version,
        group_name=group.name,
        group_definition=dict(group.definition),
        client_count=total_count,
        money_total_kes=money_total,
        evidence=group_evidence(group, thresholds),
        reason=_reason(action, group, included_count=included_count, total_count=total_count),
        angle=action.message_angle,
        content_mix=action.content_mix,
        response_kind=action.response_kind,
        permission_applied=effective_permission(
            session,
            action.action_code,
            money_total_kes=money_total,
            money_ceiling_kes=action.money_ceiling_kes,
        ),
        skip_reason_counts=skip_reason_counts(skip_reasons),
        status="proposed",
    )
    session.add(proposal)
    session.flush()

    session.add_all(
        AgentProposalClient(
            proposal_id=proposal.proposal_id,
            client_id=member.client_id,
            unit_fund_id=member.unit_fund_id,
            included=skip_reasons[member_key(member)] is None,
            skip_reason=skip_reasons[member_key(member)],
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


GROUP_EVIDENCE: dict[str, str] = {
    GRADUAL_DEPOSITOR_DUE: (
        "have not bought recently, have not withdrawn, and are past their own usual gap "
        "between deposits."
    ),
    GRADUAL_DEPOSITOR_WITHDRAWING: "have not bought recently and have taken money out.",
    FREQUENT_DEPOSITOR_ONE_FUND: "buy regularly, have not withdrawn, and hold only one fund.",
    FREQUENT_DEPOSITOR_WITHDRAWING: (
        "buy regularly but have taken out as much as they put in, or more."
    ),
    DEPOSIT_SIZE_FALLING: "have deposits that are getting smaller over time.",
    FIRST_DEPOSIT_NO_REPEAT: "made one deposit recently and have not come back.",
    FA_CALL_OVERDUE: "have a call their advisor promised that is now past its date.",
    RISK_ROSE_NO_CALL: "moved to a worse risk band in the last run with no response since.",
    APPROACHING_PRESERVATION_LINE: (
        "are on course to fall below the active line within a few months."
    ),
}


def group_evidence(group: WatchGroup, thresholds: WatchlistThresholds) -> str:
    """The plain language evidence behind one group, shared with the model driven flow."""
    count = group.client_count
    evidence = GROUP_EVIDENCE.get(group.name)
    if evidence is None:
        return f"{count} clients matched the '{group.name}' group."
    return f"{count} clients {evidence}"


# "mildest" used to be called "just starting to turn" on screen, which implied
# a reversal this metric cannot show -- it only ranks how fast deposits are
# shrinking, so the wording stays about pace, not direction.
_DEPOSIT_TREND_THIRDS = ("sharpest", "middle", "mildest")
_DEPOSIT_TREND_THIRD_TITLES = {
    "sharpest": "Declining sharply",
    "middle": "Declining moderately",
    "mildest": "Declining slowly",
}


def _split_into_thirds(ranked: list) -> list[list]:
    n = len(ranked)
    first, second = round(n / 3), round(2 * n / 3)
    return [ranked[:first], ranked[first:second], ranked[second:]]


def group_evidence_detail(session: Session, group: WatchGroup) -> dict[str, Any] | None:
    """How a group's own members split by severity, for situations where a
    real metric exists behind the evidence sentence. None when there is no
    such metric for this situation, or too few members carry it to rank.

    Ranked by each member's own deposit trend rather than a fixed cutoff, so
    this always describes tonight's group honestly instead of a guessed
    universal threshold.
    """
    if group.name != DEPOSIT_SIZE_FALLING:
        return None

    keys = {(member.client_id, member.unit_fund_id) for member in group.members}
    fund_ids = {member.unit_fund_id for member in group.members}
    if not fund_ids:
        return None
    rows = [
        row
        for row in session.execute(
            select(
                ActiveClientFund.client_id,
                ActiveClientFund.unit_fund_id,
                ActiveClientFund.deposit_trend,
            ).where(
                ActiveClientFund.unit_fund_id.in_(fund_ids),
                ActiveClientFund.deposit_trend.isnot(None),
            )
        ).all()
        if (row.client_id, row.unit_fund_id) in keys
    ]
    if len(rows) < 3:
        return None

    balance_by_key = {(m.client_id, m.unit_fund_id): m.balance for m in group.members}
    ranked = sorted(rows, key=lambda row: row.deposit_trend)
    thirds = []
    for label, chunk in zip(_DEPOSIT_TREND_THIRDS, _split_into_thirds(ranked), strict=True):
        trend_low = round(min(row.deposit_trend for row in chunk), 2)
        trend_high = round(max(row.deposit_trend for row in chunk), 2)
        thirds.append(
            {
                "label": label,
                "title": _DEPOSIT_TREND_THIRD_TITLES[label],
                "description": (
                    f"Deposit trend score {trend_low} to {trend_high} within this group "
                    "(more negative means a faster fall)."
                ),
                "client_count": len(chunk),
                "balance_kes": sum(
                    balance_by_key[(row.client_id, row.unit_fund_id)] for row in chunk
                ),
                "trend_low": trend_low,
                "trend_high": trend_high,
            }
        )
    return {
        "metric": "deposit_trend",
        "ranked_on": len(rows),
        "not_ranked": len(keys) - len(rows),
        "thirds": thirds,
    }


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
