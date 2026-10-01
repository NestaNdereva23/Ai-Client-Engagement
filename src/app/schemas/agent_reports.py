from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ReportRun(BaseModel):
    run_id: int
    agent_kind: str
    trigger: str
    state: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    failure_reason: str | None
    summary: str | None
    insight_id: int | None


class ReportGroup(BaseModel):
    group_name: str
    looked_at: bool
    outcome: str | None
    insight_count: int


class ReportCoverage(BaseModel):
    groups_total: int | None
    groups_looked_at: int
    groups: list[ReportGroup]


class ReportFinding(BaseModel):
    insight_id: int
    title: str
    kind: str
    group_name: str
    client_count: int
    confidence: str
    state: str
    dismissed_reason: str | None
    decided_by: str | None
    decided_at: datetime | None


class ReportFindings(BaseModel):
    found: int
    accepted: int
    dismissed: int
    expired: int
    undecided: int
    items: list[ReportFinding]
    acted_on: ReportFinding | None


class ReportMessages(BaseModel):
    drafted: int
    pending_review: int
    approved: int
    rejected: int
    escalated: int
    held: int
    edited: int
    sent: int
    recorded_only: int


class ReportAction(BaseModel):
    proposal_id: int
    action_code: str
    group_name: str
    insight_id: int | None
    response_kind: str | None
    status: str
    permission_applied: str
    decided_by: str | None
    content_mix: str | None
    group_size: int
    included: int
    skipped: int
    skip_reasons: dict[str, int]
    stopped_unsent: int
    messages: ReportMessages


class ReportMix(BaseModel):
    content_mix: str
    actions: int
    drafted: int
    sent: int


class ReportSkipReason(BaseModel):
    reason: str
    count: int
    by_action: dict[str, int]


class ReportWaiting(BaseModel):
    drafts_in_review: int
    new_findings: int
    proposals_awaiting_approval: int
    earlier_findings: int
    earlier_findings_oldest: datetime | None
    earlier_proposals: int
    earlier_proposals_oldest: datetime | None
    total: int


class ReportPermissionChange(BaseModel):
    changed_at: datetime
    changed_by: str | None
    priority_tier: str | None
    risk_band: str | None
    from_level: str | None
    to_level: str | None
    reason: str | None


class ReportLevel(BaseModel):
    action_code: str
    level: str
    effective_level: str
    paused: bool
    used_in_run: list[str]
    max_clients_per_day: int | None
    max_money_kes: float | None
    money_ceiling_kes: float | None
    used_clients_today: int
    used_money_today_kes: float
    history: list[ReportPermissionChange]


class ReportLimits(BaseModel):
    daily_send_limit: int | None
    used_clients_today: int
    first_run_limit: int
    force_approve_each: bool


class ReportCost(BaseModel):
    agent_kes: float | None
    sends_kes: float | None
    total_kes: float | None


class ReportEvents(BaseModel):
    paused: int
    resumed: int
    warnings: int
    errors: int


class ReportTotals(BaseModel):
    found: int
    accepted: int
    dismissed: int
    undecided: int
    drafted: int
    sent: int
    waiting_on_a_person: int
    left_out: int
    stopped_unsent: int
    cost_kes: float | None


class RunReportOut(BaseModel):
    generated_at: datetime
    run: ReportRun
    totals: ReportTotals
    coverage: ReportCoverage
    findings: ReportFindings
    actions: list[ReportAction]
    mixes: list[ReportMix]
    skipped: list[ReportSkipReason]
    waiting: ReportWaiting
    levels: list[ReportLevel]
    limits: ReportLimits
    cost: ReportCost
    events: ReportEvents
