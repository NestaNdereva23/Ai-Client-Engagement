"""The tools that let the agent record a decision and start real work.

Four things the agent may do once a person has accepted a finding: write the
proposed response down, start the work an approved proposal describes, record
that nothing should be done and why, or leave a note against a client fund so
somebody looks at it in the morning.

Three rules hold this module up.

Every one of these writes its audit row before it returns, so the record of
what the agent did exists whether or not anyone reads the answer.

No tool here can go around a check. A check that says no comes back as a
plain refusal the model can read, and there is no argument anywhere that
turns a check off.

Nothing here sends. Starting the work means the campaign exists, the group is
enrolled, and the drafts are sitting in the review queue waiting for a person.
Sending stays where it was, behind the send gate, and no path out of this
module reaches the mailer.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_brief import proposal_prohibitions
from app.agents.agent_loop import GroupDecision
from app.agents.email_agent import build_system_prompt
from app.agents.email_channel import build_default_agent
from app.agents.events import NO_EVENTS, EventLog
from app.agents.insight_members import resolve_insight_members
from app.agents.insight_proposal import ACCEPTED, gate_members, save_insight_proposal
from app.agents.proposal_state import transition_proposal
from app.agents.propose import (
    DO_NOTHING_ACTION,
    ProposalActionMissing,
    load_action_or_raise,
    member_key,
    member_skip_reason,
)
from app.agents.watchlist import GroupMember
from app.audit.log import record_audit
from app.campaigns.enrollment import enroll_cohort
from app.campaigns.generation import generate_for_enrollment
from app.campaigns.nurture_bridge import prepare_client_for_drafting
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.touch import run_due_enrollments
from app.config import Settings, get_settings
from app.db.models.active_clients import FLAGGED_FOR_ACCOUNT_MANAGER, ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.campaigns import CampaignStep
from app.db.models.outreach import Campaign
from app.privacy.llm_client import ToolSpec
from app.services.active_clients import ActiveClientNotFound, record_interaction

logger = structlog.get_logger(__name__)

WRITE_PROPOSAL = "write_proposal"
RUN_PROPOSAL = "run_proposal"
RECORD_NO_ACTION = "record_no_action"
FLAG_FOR_ACCOUNT_MANAGER = "flag_for_account_manager"

WRITE_TOOL_NAMES: tuple[str, ...] = (
    WRITE_PROPOSAL,
    RUN_PROPOSAL,
    RECORD_NO_ACTION,
    FLAG_FOR_ACCOUNT_MANAGER,
)

APPROVED = "approved"
RUNNING = "running"

NO_ACTION_DECIDED = "no_action_decided"

AGENT_ACTOR = "agent"

CAMPAIGN_TYPE = "agent_proposal"

Drafter = Callable[..., int]


def _refuse(error: str, message: str) -> dict[str, Any]:
    return {"error": error, "message": message}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _members_of(session: Session, proposal_id: int) -> list[GroupMember]:
    """The client funds a proposal took in, with the balance they hold now."""
    rows = session.scalars(
        select(AgentProposalClient).where(
            AgentProposalClient.proposal_id == proposal_id,
            AgentProposalClient.included.is_(True),
        )
    ).all()
    balances = {
        (fund.client_id, fund.unit_fund_id): fund.balance
        for fund in session.execute(
            select(
                ActiveClientFund.client_id,
                ActiveClientFund.unit_fund_id,
                ActiveClientFund.balance,
            ).where(ActiveClientFund.unit_fund_id.in_({row.unit_fund_id for row in rows}))
        ).all()
    }
    return [
        GroupMember(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=float(balances.get((row.client_id, row.unit_fund_id)) or 0.0),
        )
        for row in rows
    ]


def draft_into_review_queue(
    session: Session,
    *,
    campaign_id: int,
    prohibitions: Sequence[str],
    settings: Settings | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> int:
    """Draft the campaign's due enrollments and leave them in the review queue.

    Every draft ends as a message waiting on a person, which is exactly
    where the review queue picks it up. What the finding said a message must
    not claim is bound into the drafting prompt here, so an internal label
    cannot travel from the finding to an inbox.
    """
    settings = settings or get_settings()
    agent = build_default_agent(
        session,
        settings,
        prompt_builder=functools.partial(
            build_system_prompt, extra_prohibitions=tuple(prohibitions)
        ),
    )
    generate = functools.partial(generate_for_enrollment, agent=agent, settings=settings)
    outcomes = run_due_enrollments(session, campaign_id=campaign_id, generate=generate, limit=limit)
    return sum(1 for outcome in outcomes if outcome.generated)


def make_write_tools(
    *,
    run_id: int | None = None,
    as_of: date | None = None,
    cooldown_days: int | None = None,
    draft: Drafter = draft_into_review_queue,
    events: EventLog = NO_EVENTS,
) -> dict[str, Callable[..., dict[str, Any]]]:
    """Build the write tools for one agent run.

    Each one takes the session first and then the call's arguments, exactly
    like a registered read tool, so the executor dispatches it, scans what it
    returns and records the call the same way.
    """
    day = as_of or date.today()
    audit_run_id = None if run_id is None else str(run_id)

    def _accepted_insight(session: Session, insight_id: int) -> AgentInsight | dict[str, Any]:
        insight = session.get(AgentInsight, insight_id)
        if insight is None:
            return _refuse("unknown_insight", f"there is no finding {insight_id}")
        if insight.state != ACCEPTED:
            return _refuse(
                "insight_not_accepted",
                f"finding {insight_id} is {insight.state}, and only an accepted "
                "finding may be acted on",
            )
        return insight

    def write_proposal(
        session: Session,
        *,
        insight_id: int,
        action_code: str,
        reason: str,
        angle: str | None = None,
    ) -> dict[str, Any]:
        insight = _accepted_insight(session, insight_id)
        if isinstance(insight, dict):
            return insight
        if not _text(reason):
            return _refuse("missing_reason", "say why this response fits this finding")

        try:
            action = load_action_or_raise(session, action_code, day)
        except ProposalActionMissing as exc:
            return _refuse("unknown_action", str(exc))
        if angle != action.message_angle:
            return _refuse(
                "wrong_angle",
                f"'{action_code}' takes the angle {action.message_angle!r}, not {angle!r}",
            )

        resolved = resolve_insight_members(session, insight, day)
        if resolved.refusal is not None:
            return _refuse("no_clients_found", resolved.refusal)

        skip_reasons = gate_members(
            session,
            action=action,
            members=resolved.members,
            as_of=day,
            cooldown_days=cooldown_days,
        )
        included = tuple(
            member for member in resolved.members if skip_reasons[member_key(member)] is None
        )
        if not included:
            return _refuse(
                "every_client_was_left_out",
                f"every client in '{insight.group_name}' was left out by a check, "
                "so there is nothing to propose",
            )

        proposal = save_insight_proposal(
            session,
            insight=insight,
            decision=GroupDecision(
                action=action,
                angle=angle,
                reason=_text(reason),
                included=included,
                skip_reasons=skip_reasons,
            ),
            members=resolved.members,
            run_id=run_id,
            events=events,
        )
        record_audit(
            session,
            entity_type="agent_run",
            action=WRITE_PROPOSAL,
            entity_id=str(run_id),
            run_id=audit_run_id,
            detail={
                "insight_id": insight_id,
                "proposal_id": proposal.proposal_id,
                "action_code": action_code,
                "included_count": len(included),
            },
        )
        logger.info(
            "agent_write_tool.write_proposal",
            run_id=run_id,
            insight_id=insight_id,
            proposal_id=proposal.proposal_id,
            action_code=action_code,
            included_count=len(included),
        )
        return {
            "status": "proposed",
            "proposal_id": proposal.proposal_id,
            "action_code": action_code,
            "included_count": len(included),
            "excluded_count": len(skip_reasons) - len(included),
            "skip_reason_counts": proposal.skip_reason_counts,
            "permission_applied": proposal.permission_applied,
        }

    def run_proposal(session: Session, *, proposal_id: int) -> dict[str, Any]:
        proposal = session.get(AgentProposal, proposal_id)
        if proposal is None:
            return _refuse("unknown_proposal", f"there is no proposal {proposal_id}")
        if proposal.status != APPROVED:
            return _refuse(
                "proposal_not_approved",
                f"proposal {proposal_id} is {proposal.status}, and only an approved "
                "proposal may be started",
            )
        if proposal.action_code == DO_NOTHING_ACTION:
            return _refuse("nothing_to_run", f"proposal {proposal_id} is a decision to do nothing")

        try:
            action = load_action_or_raise(session, proposal.action_code, day)
        except ProposalActionMissing as exc:
            return _refuse("unknown_action", str(exc))

        members = _members_of(session, proposal_id)
        if not members:
            return _refuse("no_clients_left", f"proposal {proposal_id} has nobody included on it")

        settings = get_settings()
        cooldown = settings.agent_contact_cooldown_days if cooldown_days is None else cooldown_days
        blocked = {
            member_key(member): member_skip_reason(session, member, action, day, cooldown)
            for member in members
        }
        still_allowed = [member for member in members if blocked[member_key(member)] is None]
        dropped = {
            reason: sum(1 for value in blocked.values() if value == reason)
            for reason in {value for value in blocked.values() if value is not None}
        }
        if not still_allowed:
            record_audit(
                session,
                entity_type="agent_proposal",
                action="run_refused",
                entity_id=str(proposal_id),
                run_id=audit_run_id,
                detail={"reason": "every client was left out by a check", "dropped": dropped},
            )
            return _refuse(
                "every_client_was_left_out",
                "every client on this proposal was left out by a check since it was "
                f"written: {dropped}",
            )

        for member in members:
            reason = blocked[member_key(member)]
            if reason is None:
                continue
            row = session.scalar(
                select(AgentProposalClient).where(
                    AgentProposalClient.proposal_id == proposal_id,
                    AgentProposalClient.client_id == member.client_id,
                    AgentProposalClient.unit_fund_id == member.unit_fund_id,
                )
            )
            row.included = False
            row.skip_reason = reason

        ready = [
            member.client_id
            for member in still_allowed
            if proposal.angle is None
            or prepare_client_for_drafting(
                session,
                member.client_id,
                angle=proposal.angle,
                chosen_by=proposal.action_code,
                catalog_version=proposal.catalog_version,
            )
        ]
        if not ready:
            return _refuse(
                "no_clients_left",
                f"nobody on proposal {proposal_id} still holds anything in the active book",
            )

        campaign = Campaign(
            name=f"{action.title} for {proposal.group_name}",
            campaign_type=CAMPAIGN_TYPE,
            cohort_definition=proposal.group_definition,
            status=RUNNING,
            start_date=day,
        )
        session.add(campaign)
        session.flush()
        session.add(
            CampaignStep(
                campaign_id=campaign.campaign_id,
                step_no=1,
                offset_days=0,
                message_angle=proposal.angle,
            )
        )
        session.flush()

        enrollments = enroll_cohort(
            session,
            campaign_id=campaign.campaign_id,
            client_ids=ready,
        )
        proposal.campaign_id = campaign.campaign_id
        transition_proposal(
            session,
            proposal,
            to_status=RUNNING,
            reason=f"campaign {campaign.campaign_id} was created and the group enrolled",
        )
        record_audit(
            session,
            entity_type="agent_proposal",
            action="run",
            entity_id=str(proposal_id),
            run_id=audit_run_id,
            detail={
                "campaign_id": campaign.campaign_id,
                "enrolled_count": len(enrollments),
                "dropped": dropped,
            },
        )
        session.commit()

        drafted = draft(
            session,
            campaign_id=campaign.campaign_id,
            prohibitions=proposal_prohibitions(session, proposal),
        )
        logger.info(
            "agent_write_tool.run_proposal",
            run_id=run_id,
            proposal_id=proposal_id,
            campaign_id=campaign.campaign_id,
            enrolled_count=len(enrollments),
            drafted_count=drafted,
        )
        return {
            "status": RUNNING,
            "proposal_id": proposal_id,
            "campaign_id": campaign.campaign_id,
            "enrolled_count": len(enrollments),
            "dropped_since_proposed": dropped,
            "drafted_count": drafted,
            "note": "the drafts are waiting in the review queue and nothing has been sent",
        }

    def record_no_action(session: Session, *, insight_id: int, reason: str) -> dict[str, Any]:
        insight = _accepted_insight(session, insight_id)
        if isinstance(insight, dict):
            return insight
        if not _text(reason):
            return _refuse("missing_reason", "say why nothing should be done about this finding")

        try:
            action = load_action_or_raise(session, DO_NOTHING_ACTION, day)
        except ProposalActionMissing as exc:
            return _refuse("unknown_action", str(exc))

        resolved = resolve_insight_members(session, insight, day)
        proposal = save_insight_proposal(
            session,
            insight=insight,
            decision=GroupDecision(
                action=action,
                angle=None,
                reason=_text(reason),
                included=(),
                skip_reasons={member_key(member): NO_ACTION_DECIDED for member in resolved.members},
            ),
            members=resolved.members,
            run_id=run_id,
            events=events,
        )
        record_audit(
            session,
            entity_type="agent_run",
            action=RECORD_NO_ACTION,
            entity_id=str(run_id),
            run_id=audit_run_id,
            detail={
                "insight_id": insight_id,
                "proposal_id": proposal.proposal_id,
                "group_name": insight.group_name,
                "client_count": proposal.client_count,
                "reason": _text(reason),
            },
        )
        logger.info(
            "agent_write_tool.record_no_action",
            run_id=run_id,
            insight_id=insight_id,
            proposal_id=proposal.proposal_id,
            client_count=proposal.client_count,
        )
        return {
            "status": "recorded",
            "proposal_id": proposal.proposal_id,
            "group_name": insight.group_name,
            "client_count": proposal.client_count,
            "reason": _text(reason),
        }

    def flag_for_account_manager(
        session: Session, *, client_id: int, unit_fund_id: int, note: str
    ) -> dict[str, Any]:
        if not _text(note):
            return _refuse("missing_note", "say what the account manager should look at")
        try:
            row = record_interaction(
                session,
                client_id,
                unit_fund_id,
                type=FLAGGED_FOR_ACCOUNT_MANAGER,
                note=_text(note),
                reviewer_id=AGENT_ACTOR,
            )
        except ActiveClientNotFound:
            return _refuse(
                "unknown_client_fund",
                "there is no active client fund with that client and fund",
            )
        logger.info(
            "agent_write_tool.flag_for_account_manager",
            run_id=run_id,
            interaction_id=row.id,
        )
        return {
            "status": "flagged",
            "interaction_id": row.id,
            "note": row.note,
            "note_shows_on": "the morning list for whoever owns this client",
        }

    return {
        WRITE_PROPOSAL: write_proposal,
        RUN_PROPOSAL: run_proposal,
        RECORD_NO_ACTION: record_no_action,
        FLAG_FOR_ACCOUNT_MANAGER: flag_for_account_manager,
    }


WRITE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=WRITE_PROPOSAL,
        description=(
            "Write down the response you have decided on for one accepted finding, "
            "with the reason. This only proposes it. Nothing is sent, and a person "
            "still decides."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "insight_id": {"type": "integer", "description": "The finding this answers."},
                "action_code": {
                    "type": "string",
                    "description": "One response code from the action catalogue.",
                },
                "angle": {
                    "type": ["string", "null"],
                    "description": "The response's exact angle, or null when it sends nothing.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this response fits this finding, in plain everyday words.",
                },
            },
            "required": ["insight_id", "action_code", "reason"],
        },
    ),
    ToolSpec(
        name=RUN_PROPOSAL,
        description=(
            "Start the work an approved proposal describes: create the campaign, "
            "enroll the group, and draft the messages into the review queue. This "
            "sends nothing. A proposal nobody has approved is refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "integer",
                    "description": "The approved proposal to start.",
                }
            },
            "required": ["proposal_id"],
        },
    ),
    ToolSpec(
        name=RECORD_NO_ACTION,
        description=(
            "Record that nothing should be done about a finding, and why. The group "
            "and how many clients are in it are kept with the reason."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "insight_id": {"type": "integer", "description": "The finding you are closing."},
                "reason": {
                    "type": "string",
                    "description": "Why nothing should be done about it.",
                },
            },
            "required": ["insight_id", "reason"],
        },
    ),
    ToolSpec(
        name=FLAG_FOR_ACCOUNT_MANAGER,
        description=(
            "Leave a note against one client fund so the person who owns it sees it "
            "on the morning list. This contacts nobody."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "integer", "description": "The client's internal id."},
                "unit_fund_id": {"type": "integer", "description": "The fund's internal id."},
                "note": {
                    "type": "string",
                    "description": "What the account manager should look at, in plain words.",
                },
            },
            "required": ["client_id", "unit_fund_id", "note"],
        },
    ),
)
