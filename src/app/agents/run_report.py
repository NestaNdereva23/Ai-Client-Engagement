from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agents.action_catalog import load_active_actions
from app.agents.permissions import effective_permission, resolve_permission
from app.config import get_settings
from app.db.models.agent import CONTACTING_RESPONSE_KINDS, AgentActionCatalog
from app.db.models.agent_event import ERROR, PAUSED, RESUMED, RUN_STATUS, WARNING, AgentEvent
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_permission import DEFAULT_PERMISSION, AgentPermission
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.agent_run import AgentRun
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.outreach import OutreachMessage, ReviewAction
from app.schemas.agent_reports import (
    ReportAction,
    ReportCost,
    ReportCoverage,
    ReportEvents,
    ReportFinding,
    ReportFindings,
    ReportGroup,
    ReportLevel,
    ReportLimits,
    ReportMessages,
    ReportMix,
    ReportPermissionChange,
    ReportRun,
    ReportSkipReason,
    ReportTotals,
    ReportWaiting,
    RunReportOut,
)
from app.services.agent_events import GROUP_DONE, run_progress
from app.services.agent_proposals import daily_usage, overall_daily_usage

PERMISSION_ENTITY = "agent_permission"
ACCEPTED_STATES = ("accepted", "acted_on")
SENT_STATUS = "sent"
EDITED_OUTCOME = "edit_approve"
STOPPED_STATUS = "stopped"
AWAITING_APPROVAL_STATUS = "proposed"


def build_run_report(session: Session, run: AgentRun) -> RunReportOut:
    run_day = _run_day(run)
    created = _created_findings(session, run)
    proposals = _proposals_for(session, run, [row.insight_id for row in created])
    actions = [_action_row(session, proposal) for proposal in proposals]
    findings = _findings(session, run, created)
    skipped = _skipped(actions)
    waiting = _waiting(session, run, created, proposals, actions)
    cost = _cost(session, run, proposals)
    return RunReportOut(
        generated_at=datetime.now(UTC),
        run=_run_block(run),
        totals=_totals(findings, actions, skipped, waiting, cost),
        coverage=_coverage(session, run),
        findings=findings,
        actions=actions,
        mixes=_mixes(actions),
        skipped=skipped,
        waiting=waiting,
        levels=_levels(session, proposals, run_day),
        limits=_limits(session, run_day),
        cost=cost,
        events=_events(session, run),
    )


def _run_day(run: AgentRun) -> date:
    return run.started_at.astimezone(ZoneInfo(get_settings().db_timezone)).date()


def _run_block(run: AgentRun) -> ReportRun:
    duration = None
    if run.finished_at is not None:
        duration = (run.finished_at - run.started_at).total_seconds()
    return ReportRun(
        run_id=run.run_id,
        agent_kind=run.agent_kind,
        trigger=run.trigger,
        state=run.state,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=duration,
        failure_reason=run.failure_reason,
        summary=run.summary,
        insight_id=run.insight_id,
    )


def _created_findings(session: Session, run: AgentRun) -> list[AgentInsight]:
    return list(
        session.scalars(
            select(AgentInsight)
            .where(AgentInsight.run_id == run.run_id)
            .order_by(AgentInsight.insight_id)
        )
    )


def _proposals_for(
    session: Session, run: AgentRun, insight_ids: Sequence[int]
) -> list[AgentProposal]:
    clauses = [AgentProposal.run_id == run.run_id]
    if insight_ids:
        clauses.append(AgentProposal.insight_id.in_(list(insight_ids)))
    return list(
        session.scalars(
            select(AgentProposal).where(or_(*clauses)).order_by(AgentProposal.proposal_id)
        )
    )


def _finding(row: AgentInsight) -> ReportFinding:
    return ReportFinding(
        insight_id=row.insight_id,
        title=row.title,
        kind=row.kind,
        group_name=row.group_name,
        client_count=row.client_count,
        confidence=row.confidence,
        state=row.state,
        dismissed_reason=row.dismissed_reason,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )


def _findings(session: Session, run: AgentRun, created: Sequence[AgentInsight]) -> ReportFindings:
    acted_on = session.get(AgentInsight, run.insight_id) if run.insight_id is not None else None
    return ReportFindings(
        found=len(created),
        accepted=sum(1 for row in created if row.state in ACCEPTED_STATES),
        dismissed=sum(1 for row in created if row.state == "dismissed"),
        expired=sum(1 for row in created if row.state == "expired"),
        undecided=sum(1 for row in created if row.state == "new"),
        items=[_finding(row) for row in created],
        acted_on=None if acted_on is None else _finding(acted_on),
    )


def _skip_reasons(session: Session, proposal_id: int) -> tuple[int, dict[str, int]]:
    rows = session.execute(
        select(
            AgentProposalClient.included,
            AgentProposalClient.skip_reason,
            func.count(),
        )
        .where(AgentProposalClient.proposal_id == proposal_id)
        .group_by(AgentProposalClient.included, AgentProposalClient.skip_reason)
    ).all()
    included = sum(count for is_included, _, count in rows if is_included)
    reasons = {reason: count for is_included, reason, count in rows if not is_included and reason}
    return included, reasons


def _message_statuses(session: Session, campaign_id: int) -> dict[str, int]:
    rows = session.execute(
        select(OutreachMessage.status, func.count())
        .where(OutreachMessage.campaign_id == campaign_id)
        .group_by(OutreachMessage.status)
    ).all()
    return dict(rows)


def _edited_count(session: Session, campaign_id: int) -> int:
    return (
        session.scalar(
            select(func.count(func.distinct(ReviewAction.message_id)))
            .join(OutreachMessage, OutreachMessage.message_id == ReviewAction.message_id)
            .where(
                OutreachMessage.campaign_id == campaign_id,
                ReviewAction.outcome == EDITED_OUTCOME,
            )
        )
        or 0
    )


def _touch_statuses(session: Session, campaign_id: int) -> dict[str | None, int]:
    rows = session.execute(
        select(TouchLog.delivery_status, func.count())
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .where(Enrollment.campaign_id == campaign_id, TouchLog.sent_at.is_not(None))
        .group_by(TouchLog.delivery_status)
    ).all()
    return dict(rows)


def _messages(session: Session, campaign_id: int | None) -> ReportMessages:
    if campaign_id is None:
        return ReportMessages(
            drafted=0,
            pending_review=0,
            approved=0,
            rejected=0,
            escalated=0,
            held=0,
            edited=0,
            sent=0,
            recorded_only=0,
        )
    statuses = _message_statuses(session, campaign_id)
    touches = _touch_statuses(session, campaign_id)
    sent = touches.get(SENT_STATUS, 0)
    return ReportMessages(
        drafted=sum(statuses.values()),
        pending_review=statuses.get("pending_review", 0),
        approved=statuses.get("approved", 0),
        rejected=statuses.get("rejected", 0),
        escalated=statuses.get("escalated", 0),
        held=statuses.get("held", 0),
        edited=_edited_count(session, campaign_id),
        sent=sent,
        recorded_only=sum(touches.values()) - sent,
    )


def _action_row(session: Session, proposal: AgentProposal) -> ReportAction:
    included, reasons = _skip_reasons(session, proposal.proposal_id)
    messages = _messages(session, proposal.campaign_id)
    stopped_unsent = 0
    if proposal.status == STOPPED_STATUS:
        stopped_unsent = max(included - messages.sent, 0)
    return ReportAction(
        proposal_id=proposal.proposal_id,
        action_code=proposal.action_code,
        group_name=proposal.group_name,
        insight_id=proposal.insight_id,
        response_kind=proposal.response_kind,
        status=proposal.status,
        permission_applied=proposal.permission_applied,
        decided_by=proposal.decided_by,
        content_mix=proposal.content_mix,
        group_size=proposal.client_count,
        included=included,
        skipped=sum(reasons.values()),
        skip_reasons=reasons,
        stopped_unsent=stopped_unsent,
        messages=messages,
    )


def _mixes(actions: Sequence[ReportAction]) -> list[ReportMix]:
    counts: dict[str, list[int]] = {}
    for action in actions:
        if action.content_mix is None or action.response_kind not in CONTACTING_RESPONSE_KINDS:
            continue
        row = counts.setdefault(action.content_mix, [0, 0, 0])
        row[0] += 1
        row[1] += action.messages.drafted
        row[2] += action.messages.sent
    return [
        ReportMix(content_mix=mix, actions=acted, drafted=drafted, sent=sent)
        for mix, (acted, drafted, sent) in sorted(counts.items())
    ]


def _skipped(actions: Sequence[ReportAction]) -> list[ReportSkipReason]:
    by_reason: dict[str, dict[str, int]] = {}
    for action in actions:
        for reason, count in action.skip_reasons.items():
            per_action = by_reason.setdefault(reason, {})
            per_action[action.action_code] = per_action.get(action.action_code, 0) + count
    rows = [
        ReportSkipReason(reason=reason, count=sum(per_action.values()), by_action=per_action)
        for reason, per_action in by_reason.items()
    ]
    return sorted(rows, key=lambda row: (-row.count, row.reason))


def _waiting(
    session: Session,
    run: AgentRun,
    created: Sequence[AgentInsight],
    proposals: Sequence[AgentProposal],
    actions: Sequence[ReportAction],
) -> ReportWaiting:
    proposal_ids = [proposal.proposal_id for proposal in proposals]
    earlier_findings, earlier_findings_oldest = session.execute(
        select(func.count(), func.min(AgentInsight.created_at)).where(
            AgentInsight.state == "new",
            or_(AgentInsight.run_id.is_(None), AgentInsight.run_id != run.run_id),
        )
    ).one()
    earlier_proposal_clauses = [AgentProposal.status == AWAITING_APPROVAL_STATUS]
    if proposal_ids:
        earlier_proposal_clauses.append(AgentProposal.proposal_id.not_in(proposal_ids))
    earlier_proposals, earlier_proposals_oldest = session.execute(
        select(func.count(), func.min(AgentProposal.created_at)).where(*earlier_proposal_clauses)
    ).one()
    drafts = sum(action.messages.pending_review for action in actions)
    new_findings = sum(1 for row in created if row.state == "new")
    awaiting = sum(1 for proposal in proposals if proposal.status == AWAITING_APPROVAL_STATUS)
    return ReportWaiting(
        drafts_in_review=drafts,
        new_findings=new_findings,
        proposals_awaiting_approval=awaiting,
        earlier_findings=earlier_findings,
        earlier_findings_oldest=earlier_findings_oldest,
        earlier_proposals=earlier_proposals,
        earlier_proposals_oldest=earlier_proposals_oldest,
        total=drafts + new_findings + awaiting + earlier_findings + earlier_proposals,
    )


def _cost(session: Session, run: AgentRun, proposals: Sequence[AgentProposal]) -> ReportCost:
    campaign_ids = [p.campaign_id for p in proposals if p.campaign_id is not None]
    sends = None
    if campaign_ids:
        total = session.scalar(
            select(func.sum(TouchLog.cost))
            .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
            .where(Enrollment.campaign_id.in_(campaign_ids), TouchLog.cost.is_not(None))
        )
        sends = None if total is None else float(total)
    parts = [part for part in (run.cost_kes, sends) if part is not None]
    return ReportCost(
        agent_kes=run.cost_kes,
        sends_kes=sends,
        total_kes=sum(parts) if parts else None,
    )


def _totals(
    findings: ReportFindings,
    actions: Sequence[ReportAction],
    skipped: Sequence[ReportSkipReason],
    waiting: ReportWaiting,
    cost: ReportCost,
) -> ReportTotals:
    return ReportTotals(
        found=findings.found,
        accepted=findings.accepted,
        dismissed=findings.dismissed,
        undecided=findings.undecided,
        drafted=sum(action.messages.drafted for action in actions),
        sent=sum(action.messages.sent for action in actions),
        waiting_on_a_person=waiting.total,
        left_out=sum(row.count for row in skipped),
        stopped_unsent=sum(action.stopped_unsent for action in actions),
        cost_kes=cost.total_kes,
    )


def _gathered_groups(session: Session, run: AgentRun) -> list[str]:
    detail = session.scalar(
        select(AuditLog.detail)
        .where(
            AuditLog.entity_type == "agent_run",
            AuditLog.action == "gather",
            AuditLog.run_id == str(run.run_id),
        )
        .order_by(AuditLog.log_id.desc())
        .limit(1)
    )
    return list((detail or {}).get("group_names", []))


def _finished_groups(session: Session, run: AgentRun) -> dict[str, dict]:
    rows = session.scalars(
        select(AgentEvent.detail)
        .where(
            AgentEvent.run_id == run.run_id,
            AgentEvent.kind == RUN_STATUS,
            AgentEvent.detail["stage"].astext == GROUP_DONE,
        )
        .order_by(AgentEvent.ordinal)
    )
    return {detail.get("group_name"): detail for detail in rows}


def _coverage(session: Session, run: AgentRun) -> ReportCoverage:
    progress = run_progress(session, run.run_id)
    finished = _finished_groups(session, run)
    names = _gathered_groups(session, run)
    names += [name for name in finished if name not in names]
    groups = [
        ReportGroup(
            group_name=name,
            looked_at=name in finished,
            outcome=finished.get(name, {}).get("outcome"),
            insight_count=int(finished.get(name, {}).get("insight_count", 0)),
        )
        for name in names
    ]
    return ReportCoverage(
        groups_total=progress.groups_total,
        groups_looked_at=progress.groups_looked_at,
        groups=groups,
    )


def _permission_history(session: Session, action_code: str) -> list[ReportPermissionChange]:
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == PERMISSION_ENTITY,
            AuditLog.detail["action_code"].astext == action_code,
        )
        .order_by(AuditLog.created_at, AuditLog.log_id)
    )
    changes = []
    for row in rows:
        detail = row.detail or {}
        changes.append(
            ReportPermissionChange(
                changed_at=row.created_at,
                changed_by=row.actor_id,
                priority_tier=detail.get("priority_tier"),
                risk_band=detail.get("risk_band"),
                from_level=(detail.get("before") or {}).get("permission"),
                to_level=(detail.get("after") or {}).get("permission"),
                reason=detail.get("reason"),
            )
        )
    return changes


def _action_codes(
    session: Session,
    catalog: dict[str, AgentActionCatalog],
    proposals: Sequence[AgentProposal],
) -> list[str]:
    stored = session.scalars(select(AgentPermission.action_code).distinct())
    codes = set(catalog) | set(stored) | {proposal.action_code for proposal in proposals}
    return sorted(codes)


def _level(
    session: Session,
    action_code: str,
    catalog: dict[str, AgentActionCatalog],
    proposals: Sequence[AgentProposal],
    run_day: date,
) -> ReportLevel:
    stored = resolve_permission(session, action_code)
    entry = catalog.get(action_code)
    usage = daily_usage(session, action_code=action_code, as_of=run_day)
    return ReportLevel(
        action_code=action_code,
        level=DEFAULT_PERMISSION if stored is None else stored.permission,
        effective_level=effective_permission(session, action_code),
        paused=entry is None or entry.paused,
        used_in_run=sorted(
            {p.permission_applied for p in proposals if p.action_code == action_code}
        ),
        max_clients_per_day=None if stored is None else stored.max_clients_per_day,
        max_money_kes=None if stored is None else stored.max_money_kes,
        money_ceiling_kes=None if entry is None else entry.money_ceiling_kes,
        used_clients_today=usage.used_clients,
        used_money_today_kes=usage.used_money_kes,
        history=_permission_history(session, action_code),
    )


def _levels(
    session: Session, proposals: Sequence[AgentProposal], run_day: date
) -> list[ReportLevel]:
    catalog = load_active_actions(session, run_day)
    return [
        _level(session, code, catalog, proposals, run_day)
        for code in _action_codes(session, catalog, proposals)
    ]


def _limits(session: Session, run_day: date) -> ReportLimits:
    settings = get_settings()
    return ReportLimits(
        daily_send_limit=settings.agent_daily_send_limit,
        used_clients_today=overall_daily_usage(session, as_of=run_day).used_clients,
        first_run_limit=settings.agent_first_run_limit,
        force_approve_each=settings.agent_force_approve_each,
    )


def _events(session: Session, run: AgentRun) -> ReportEvents:
    rows = dict(
        session.execute(
            select(AgentEvent.kind, func.count())
            .where(
                AgentEvent.run_id == run.run_id,
                AgentEvent.kind.in_((PAUSED, RESUMED, WARNING, ERROR)),
            )
            .group_by(AgentEvent.kind)
        ).all()
    )
    return ReportEvents(
        paused=rows.get(PAUSED, 0),
        resumed=rows.get(RESUMED, 0),
        warnings=rows.get(WARNING, 0),
        errors=rows.get(ERROR, 0),
    )
