from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.insight_state import transition_insight
from app.agents.lifecycle_members import funds_in_state
from app.agents.lifecycle_policy import find_policy, policy_conditions
from app.agents.lifecycle_state import change_state
from app.agents.lifecycle_workflow import LIFECYCLE_ACTOR
from app.agents.query_fields import compile_conditions
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.agent_insight import LIFECYCLE_CHANGE_KIND, AgentInsight
from app.db.models.client_lifecycle import (
    APPLIED,
    EVIDENCE_NOT_MET,
    NEEDS_A_PERSON,
    NO_POLICY,
    PENDING,
    WAITING_FOR_A_PERSON,
    AgentInsightLifecycle,
    LifecyclePolicy,
)

logger = structlog.get_logger(__name__)

SETTLEABLE_STATES = ("new", "accepted")


def _record_outcome(
    session: Session,
    link: AgentInsightLifecycle,
    outcome: str,
    policy: LifecyclePolicy | None,
    clients_changed: int,
) -> str:
    link.outcome = outcome
    link.policy_code = None if policy is None else policy.policy_code
    link.settled_at = datetime.now(UTC)
    session.flush()
    record_audit(
        session,
        entity_type="agent_insight",
        entity_id=str(link.insight_id),
        action="lifecycle_outcome",
        detail={
            "outcome": outcome,
            "policy_code": link.policy_code,
            "from": link.from_state,
            "to": link.to_state,
            "clients_changed": clients_changed,
        },
    )
    return outcome


def _close_insight(session: Session, insight: AgentInsight, policy: LifecyclePolicy) -> None:
    reason = f"applied under the rule {policy.policy_code}"
    if insight.state == "new":
        transition_insight(session, insight, to_state="accepted", reason=reason)
    transition_insight(session, insight, to_state="acted_on", reason=reason)


def settle_lifecycle_insight(
    session: Session, insight: AgentInsight, *, approved_by: str | None = None
) -> str | None:
    link = session.get(AgentInsightLifecycle, insight.insight_id)
    if link is None or insight.state not in SETTLEABLE_STATES:
        return None

    policy = find_policy(session, link.from_state, link.to_state)
    if policy is None:
        return _record_outcome(session, link, NO_POLICY, None, 0)
    if policy.mode == NEEDS_A_PERSON and approved_by is None:
        return _record_outcome(session, link, WAITING_FOR_A_PERSON, policy, 0)

    clauses, _ = compile_conditions(policy_conditions(policy))
    eligible = funds_in_state(
        session, state=link.from_state, clauses=clauses, among_insight=insight.insight_id
    )
    if approved_by is None and len(eligible) > get_settings().lifecycle_auto_max_clients:
        return _record_outcome(session, link, WAITING_FOR_A_PERSON, policy, 0)
    if not eligible:
        return _record_outcome(session, link, EVIDENCE_NOT_MET, policy, 0)

    for fund in eligible:
        change_state(
            session,
            client_id=fund.client_id,
            unit_fund_id=fund.unit_fund_id,
            policy=policy,
            insight_id=insight.insight_id,
            changed_by=approved_by or LIFECYCLE_ACTOR,
        )
    _close_insight(session, insight, policy)
    return _record_outcome(session, link, APPLIED, policy, len(eligible))


def run_lifecycle_rules(session: Session, *, run_id: int | None = None) -> dict[str, int]:
    query = (
        select(AgentInsight)
        .join(AgentInsightLifecycle, AgentInsightLifecycle.insight_id == AgentInsight.insight_id)
        .where(
            AgentInsight.kind == LIFECYCLE_CHANGE_KIND,
            AgentInsight.state == "new",
            AgentInsightLifecycle.outcome == PENDING,
        )
        .order_by(AgentInsight.insight_id)
    )
    if run_id is not None:
        query = query.where(AgentInsight.run_id == run_id)

    outcomes: Counter[str] = Counter()
    for insight in session.scalars(query).all():
        outcome = settle_lifecycle_insight(session, insight)
        if outcome is not None:
            outcomes[outcome] += 1
    logger.info("lifecycle_rules.done", run_id=run_id, outcomes=dict(outcomes))
    return dict(outcomes)
