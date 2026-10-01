from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.guide_mix import MIX_LABELS
from app.agents.run_report import build_run_report
from app.audit.log import record_audit
from app.config import Settings, get_settings
from app.db.models.agent import CONTACTING_RESPONSE_KINDS
from app.db.models.agent_run import AgentRun
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.delivery.mailer import EmailMessage, Mailer, get_mailer
from app.schemas.agent_reports import (
    ReportAction,
    ReportLevel,
    ReportPermissionChange,
    RunReportOut,
)

logger = structlog.get_logger(__name__)

REPORT_ENTITY = "agent_run_report"

LEVEL_NAMES = {
    "suggest_only": "suggest only",
    "approve_each": "every message reviewed",
    "approve_sample": "sample reviewed, then the rest follow",
    "act_alone": "runs on its own",
}


@dataclass(frozen=True)
class RenderedReport:
    subject: str
    text_body: str


def _when(value: datetime, zone: str) -> str:
    return value.astimezone(ZoneInfo(zone)).strftime("%a %d %b %Y %H:%M")


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _subject(report: RunReportOut) -> str:
    totals = report.totals
    head = f"ACE run {report.run.run_id}"
    if report.run.state == "failed":
        return f"{head} failed: {totals.found} found, {totals.sent} sent"
    if report.run.state == "running":
        return f"{head} still running"
    if totals.found == 0 and totals.sent == 0 and totals.drafted == 0:
        return f"{head}: nothing new found"
    return f"{head}: {totals.found} found, {totals.sent} sent, {totals.waiting_on_a_person} waiting"


def _header(report: RunReportOut, zone: str) -> list[str]:
    run = report.run
    lines = [
        f"Run {run.run_id}: {run.agent_kind} agent, trigger {run.trigger}",
        f"State: {run.state}",
        f"Started: {_when(run.started_at, zone)}",
    ]
    if run.finished_at is not None:
        lines.append(
            f"Finished: {_when(run.finished_at, zone)} ({run.duration_seconds:.0f} seconds)"
        )
    if run.failure_reason:
        lines.append(f"The run failed: {run.failure_reason}")
    if report.events.paused:
        lines.append(f"Paused {report.events.paused} times, resumed {report.events.resumed}.")
    return lines


def _headline(report: RunReportOut) -> list[str]:
    totals = report.totals
    return [
        f"Found: {totals.found}",
        f"Accepted: {totals.accepted}",
        f"Dismissed: {totals.dismissed}",
        f"Drafted: {totals.drafted}",
        f"Sent: {totals.sent}",
        f"Waiting on a person: {totals.waiting_on_a_person}",
        f"Left out: {totals.left_out}",
        f"Stopped before sending: {totals.stopped_unsent}",
    ]


def _found(report: RunReportOut) -> list[str]:
    findings = report.findings
    coverage = report.coverage
    lines = []
    if coverage.groups_total is not None:
        lines.append(f"Groups looked at: {coverage.groups_looked_at} of {coverage.groups_total}")
        missed = [group.group_name for group in coverage.groups if not group.looked_at]
        if missed:
            lines.append(f"Not looked at: {', '.join(missed)}")
    if not findings.items:
        lines.append("Nothing new was found.")
    for item in findings.items:
        lines.append(
            f"- {item.title} ({item.group_name}, "
            f"{_plural(item.client_count, 'client fund', 'client funds')}, "
            f"{item.confidence} confidence): {item.state}"
        )
    if findings.acted_on is not None:
        lines.append(f"This run acted on: {findings.acted_on.title}")
    return lines


def _decisions(report: RunReportOut, zone: str) -> list[str]:
    decided = [item for item in report.findings.items if item.decided_at is not None]
    if not decided:
        return ["No finding from this run has been accepted or dismissed yet."]
    lines = []
    for item in decided:
        who = item.decided_by or "unknown"
        lines.append(
            f"- {item.state.capitalize()} by {who} on {_when(item.decided_at, zone)}: {item.title}"
        )
        if item.dismissed_reason:
            lines.append(f"  Reason: {item.dismissed_reason}")
    return lines


def _action_lines(action: ReportAction) -> list[str]:
    messages = action.messages
    lines = [
        f"- {action.action_code} for {action.group_name} ({action.status}, "
        f"level applied: {LEVEL_NAMES.get(action.permission_applied, action.permission_applied)})",
        f"  Group of {action.group_size}: {action.included} included, {action.skipped} left out",
        f"  Drafted {messages.drafted}, waiting for review {messages.pending_review}, "
        f"approved {messages.approved}, edited {messages.edited}, rejected {messages.rejected}",
        f"  Sent {messages.sent}",
    ]
    if action.content_mix and action.response_kind in CONTACTING_RESPONSE_KINDS:
        lines.append(f"  Guide mix: {MIX_LABELS.get(action.content_mix, action.content_mix)}")
    if messages.recorded_only:
        lines.append(f"  Recorded but not delivered: {messages.recorded_only}")
    if action.stopped_unsent:
        lines.append(f"  Stopped before sending: {action.stopped_unsent}")
    return lines


def _ran(report: RunReportOut) -> list[str]:
    if not report.actions:
        return ["Nothing ran."]
    lines = []
    for action in report.actions:
        lines.extend(_action_lines(action))
    return lines


def _mix_lines(report: RunReportOut) -> list[str]:
    if not report.mixes:
        return ["No message was drafted."]
    return [
        f"- {MIX_LABELS.get(row.content_mix, row.content_mix)}: "
        f"{_plural(row.actions, 'action', 'actions')}, drafted {row.drafted}, sent {row.sent}"
        for row in report.mixes
    ]


def _waiting(report: RunReportOut, zone: str) -> list[str]:
    waiting = report.waiting
    lines = [
        f"Drafts in the review queue: {waiting.drafts_in_review}",
        f"New findings to accept or dismiss: {waiting.new_findings}",
        f"Proposals waiting for approval: {waiting.proposals_awaiting_approval}",
        _earlier(
            "Earlier findings still undecided",
            waiting.earlier_findings,
            waiting.earlier_findings_oldest,
            zone,
        ),
        _earlier(
            "Earlier proposals still waiting",
            waiting.earlier_proposals,
            waiting.earlier_proposals_oldest,
            zone,
        ),
        f"Total waiting on a person: {waiting.total}",
    ]
    return lines


def _earlier(label: str, count: int, oldest: datetime | None, zone: str) -> str:
    if not count or oldest is None:
        return f"{label}: {count}"
    return f"{label}: {count} (oldest from {_when(oldest, zone)})"


def _skipped(report: RunReportOut) -> list[str]:
    if not report.skipped:
        return ["No client fund was left out."]
    lines = []
    for row in report.skipped:
        by_action = ", ".join(f"{code} {count}" for code, count in row.by_action.items())
        lines.append(f"- {row.reason}: {row.count} ({by_action})")
    return lines


def _level_lines(level: ReportLevel, zone: str) -> list[str]:
    name = LEVEL_NAMES.get(level.level, level.level)
    lines = [f"- {level.action_code}: {name}"]
    if level.effective_level != level.level:
        lines.append(
            f"  In force right now: {LEVEL_NAMES.get(level.effective_level, level.effective_level)}"
        )
    if level.paused:
        lines.append("  Paused")
    for change in level.history:
        lines.append(_change_line(change, zone))
    if not level.history:
        lines.append("  No recorded changes.")
    return lines


def _change_line(change: ReportPermissionChange, zone: str) -> str:
    after = LEVEL_NAMES.get(change.to_level, change.to_level) if change.to_level else "removed"
    if change.from_level:
        before = LEVEL_NAMES.get(change.from_level, change.from_level)
        what = f"moved from {before} to {after}"
    else:
        what = f"set to {after}"
    line = f"  {_when(change.changed_at, zone)}: {what} by {change.changed_by or 'unknown'}"
    return f"{line}. Reason: {change.reason}" if change.reason else line


def _levels(report: RunReportOut, zone: str) -> list[str]:
    lines = []
    for level in report.levels:
        lines.extend(_level_lines(level, zone))
    return lines or ["No actions are set up."]


def _limits(report: RunReportOut) -> list[str]:
    limits = report.limits
    cap = "no limit" if limits.daily_send_limit is None else str(limits.daily_send_limit)
    return [
        f"Daily send limit: {cap} ({limits.used_clients_today} used today)",
        f"First run limit for a new action: {limits.first_run_limit}",
        f"Every message reviewed switch: {'on' if limits.force_approve_each else 'off'}",
    ]


def _cost(report: RunReportOut) -> list[str]:
    cost = report.cost

    def line(label: str, value: float | None) -> str:
        return f"{label}: {'not recorded' if value is None else f'KES {value:.2f}'}"

    return [
        line("Agent model calls", cost.agent_kes),
        line("Message sending", cost.sends_kes),
        line("Total", cost.total_kes),
    ]


def _section(title: str, lines: list[str]) -> list[str]:
    return ["", title.upper(), *lines]


def render_run_report(report: RunReportOut, settings: Settings | None = None) -> RenderedReport:
    zone = (settings or get_settings()).db_timezone
    lines = [
        *_header(report, zone),
        *_section("Totals", _headline(report)),
        *_section("What was found", _found(report)),
        *_section("Accepted and dismissed", _decisions(report, zone)),
        *_section("What ran", _ran(report)),
        *_section("Messages by guide mix", _mix_lines(report)),
        *_section("What is waiting", _waiting(report, zone)),
        *_section("What was left out, and why", _skipped(report)),
        *_section("Level of each action, and how it got there", _levels(report, zone)),
        *_section("Limits", _limits(report)),
        *_section("Cost", _cost(report)),
        "",
        "Anything already sent cannot be pulled back.",
        "This report holds counts and group names only.",
    ]
    return RenderedReport(subject=_subject(report), text_body="\n".join(lines))


def _recipient_key(run_id: int, address: str) -> str:
    digest = hashlib.sha256(address.lower().encode()).hexdigest()[:16]
    return f"{run_id}:{digest}"


def _already_sent(session: Session, key: str) -> bool:
    return (
        session.scalar(
            select(AuditLog.log_id)
            .where(
                AuditLog.entity_type == REPORT_ENTITY,
                AuditLog.action == "send",
                AuditLog.entity_id == key,
            )
            .limit(1)
        )
        is not None
    )


def _send_one(
    session: Session, mailer: Mailer, run_id: int, address: str, rendered: RenderedReport
) -> bool:
    key = _recipient_key(run_id, address)
    try:
        result = mailer.send(
            EmailMessage(to=address, subject=rendered.subject, text_body=rendered.text_body)
        )
        record_audit(
            session,
            entity_type=REPORT_ENTITY,
            action="send",
            entity_id=key,
            run_id=str(run_id),
            detail={"status": "sent" if result.sent else "recorded", "subject": rendered.subject},
        )
        session.commit()
        return True
    except Exception as exc:
        session.rollback()
        logger.exception("run_report.send_failed", run_id=run_id)
        record_audit(
            session,
            entity_type=REPORT_ENTITY,
            action="fail",
            entity_id=key,
            run_id=str(run_id),
            detail={"error": str(exc)},
        )
        session.commit()
        return False


def send_run_report(
    run_id: int, *, settings: Settings | None = None, mailer: Mailer | None = None
) -> int:
    settings = settings or get_settings()
    recipients = settings.agent_report_recipient_list
    if not recipients:
        return 0
    with SessionLocal() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return 0
        waiting = [
            address
            for address in recipients
            if not _already_sent(session, _recipient_key(run_id, address))
        ]
        if not waiting:
            return 0
        rendered = render_run_report(build_run_report(session, run), settings)
        mailer = mailer if mailer is not None else get_mailer(settings)
        sent = sum(_send_one(session, mailer, run_id, address, rendered) for address in waiting)
        close = getattr(mailer, "close", None)
        if close is not None:
            close()
        return sent


def announce_run_finished(run_id: int) -> None:
    try:
        send_run_report(run_id)
    except Exception:
        logger.exception("run_report.announce_failed", run_id=run_id)
