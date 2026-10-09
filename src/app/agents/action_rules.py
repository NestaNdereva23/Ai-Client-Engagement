from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_results import SENT_STATUS
from app.agents.guide_mix import guides_for_action
from app.db.models.active_clients import FLAGGED_FOR_ACCOUNT_MANAGER
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.outreach import Campaign, OutreachMessage
from app.services.active_clients import ActiveClientNotFound, record_interaction

FOLLOW_UP_ACTION = "follow_up_when_no_one_called"
SECOND_FUND_ACTION = "suggest_second_fund"
LEARNING_NOTE_ACTION = "send_learning_note"

ALWAYS_NEEDS_A_PERSON = frozenset({SECOND_FUND_ACTION})
OFFERED_ONCE = frozenset({SECOND_FUND_ACTION})
NEEDS_A_GUIDE = frozenset({SECOND_FUND_ACTION, LEARNING_NOTE_ACTION})

ACCOUNT_MANAGER_NOTES: dict[str, str] = {
    FOLLOW_UP_ACTION: (
        "This client was waiting on the call list with no call logged, so a follow up "
        "email was prepared and waits for review. If you can call them first, please "
        "log the call."
    ),
}


def already_offered(session: Session, client_id: int, action: AgentActionCatalog) -> bool:
    if action.action_code not in OFFERED_ONCE:
        return False
    return (
        session.scalar(
            select(TouchLog.touch_id)
            .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
            .join(Campaign, Campaign.campaign_id == Enrollment.campaign_id)
            .join(AgentProposal, AgentProposal.campaign_id == Campaign.campaign_id)
            .where(
                Enrollment.client_id == client_id,
                AgentProposal.action_code == action.action_code,
                TouchLog.delivery_status == SENT_STATUS,
                Campaign.is_test.is_(False),
            )
            .limit(1)
        )
        is not None
    )


def lacks_an_approved_guide(session: Session, action: AgentActionCatalog) -> bool:
    return action.action_code in NEEDS_A_GUIDE and not guides_for_action(session, action)


def tell_account_managers(
    session: Session, proposal: AgentProposal, campaign_id: int, *, author: str
) -> int:
    note = ACCOUNT_MANAGER_NOTES.get(proposal.action_code)
    if note is None:
        return 0
    drafted_for = set(
        session.scalars(
            select(OutreachMessage.client_id).where(OutreachMessage.campaign_id == campaign_id)
        )
    )
    if not drafted_for:
        return 0
    funds = session.execute(
        select(AgentProposalClient.client_id, AgentProposalClient.unit_fund_id).where(
            AgentProposalClient.proposal_id == proposal.proposal_id,
            AgentProposalClient.included.is_(True),
            AgentProposalClient.client_id.in_(drafted_for),
        )
    ).all()
    told = 0
    for client_id, unit_fund_id in funds:
        try:
            record_interaction(
                session,
                client_id,
                unit_fund_id,
                type=FLAGGED_FOR_ACCOUNT_MANAGER,
                note=note,
                reviewer_id=author,
            )
        except ActiveClientNotFound:
            continue
        told += 1
    return told
