from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.api.access import book_fa_id, get_access
from app.api.reviewer_auth import bearer_scheme
from app.config import get_settings
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal
from app.db.models.agent_run import AgentRun
from app.db.session import SessionLocal, get_session
from app.schemas.review import DecideBatchRequest
from app.services.agent_campaign_scope import campaign_is_visible, message_is_visible


@dataclass(frozen=True)
class AgentScope:
    fa_id: str | None

    @property
    def is_everyone(self) -> bool:
        return self.fa_id is None

    def allows(self, row_fa_id: str | None) -> bool:
        return self.is_everyone or row_fa_id == self.fa_id


def get_agent_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> AgentScope:
    if not get_settings().agent_fa_scoped:
        return AgentScope(None)
    access = get_access(credentials, session)
    if access.sees_every_book:
        return AgentScope(None)
    return AgentScope(book_fa_id(access))


def require_insight_in_scope(
    insight_id: int,
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> None:
    if scope.is_everyone:
        return
    insight = session.get(AgentInsight, insight_id)
    if insight is not None and not scope.allows(insight.fa_id):
        raise HTTPException(status_code=404, detail="insight not found")


def require_proposal_in_scope(
    proposal_id: int,
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> None:
    if scope.is_everyone:
        return
    proposal = session.get(AgentProposal, proposal_id)
    if proposal is not None and not scope.allows(proposal.fa_id):
        raise HTTPException(status_code=404, detail="proposal not found")


def require_campaign_in_scope(
    campaign_id: int,
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> None:
    if scope.is_everyone:
        return
    if not campaign_is_visible(session, campaign_id, scope.fa_id):
        raise HTTPException(status_code=404, detail="campaign not found")


def require_message_in_scope(
    message_id: str,
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> None:
    if scope.is_everyone:
        return
    if not message_is_visible(session, message_id, scope.fa_id):
        raise HTTPException(status_code=404, detail="message not found")


def require_messages_in_scope(
    body: DecideBatchRequest,
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> None:
    if scope.is_everyone:
        return
    for message_id in body.message_ids:
        if not message_is_visible(session, message_id, scope.fa_id):
            raise HTTPException(status_code=404, detail="message not found")


def require_run_in_scope(
    run_id: int,
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> None:
    if scope.is_everyone:
        return
    run = session.get(AgentRun, run_id)
    if run is not None and not scope.allows(run.fa_id):
        raise HTTPException(status_code=404, detail="agent run not found")


def advisor_for_new_run(scope: AgentScope) -> str | None:
    if not get_settings().agent_fa_scoped:
        return None
    if scope.is_everyone:
        raise HTTPException(
            status_code=422,
            detail="pick an advisor with view as before starting a run",
        )
    return scope.fa_id


def check_run_in_scope(credentials: HTTPAuthorizationCredentials | None, run_id: int) -> None:
    with SessionLocal() as session:
        scope = get_agent_scope(credentials, session)
        require_run_in_scope(run_id, scope, session)
