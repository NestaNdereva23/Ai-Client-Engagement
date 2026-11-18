from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.briefing.weekly_report_pdf import render_advisor_report, render_lead_report
from app.config import Settings, get_settings
from app.db.models.audit import AuditLog
from app.delivery.mailer import Attachment, EmailMessage, Mailer, get_mailer
from app.services.client_touchpoints import today_in_nairobi
from app.services.user_access import (
    advisor_fa_id,
    lead_report_recipients,
    load_advisors,
    weekly_report_recipients,
)
from app.services.weekly_report import build_weekly_reports, day_label, report_week
from app.workers.digest_email import addressing

logger = structlog.get_logger(__name__)

# Off while the reports are being tested: every weekday run sends them.
MONDAY_ONLY = False
ENTITY_TYPE = "weekly_report"
KIND_ADVISOR = "advisor"
KIND_LEAD = "lead"


@dataclass(frozen=True)
class WeeklyReportRunResult:
    sent: int = 0
    already_sent: int = 0
    failed: int = 0


def _already_sent(session: Session, today: date) -> set[str]:
    return set(
        session.scalars(
            select(AuditLog.entity_id).where(
                AuditLog.entity_type == ENTITY_TYPE,
                AuditLog.action == "send",
                AuditLog.entity_id.like(f"{today.isoformat()}:%"),
            )
        )
    )


def send_weekly_reports(
    session: Session,
    *,
    today: date | None = None,
    mailer: Mailer | None = None,
    settings: Settings | None = None,
    any_day: bool = False,
    resend: bool = False,
) -> WeeklyReportRunResult:
    today = today or today_in_nairobi()
    if MONDAY_ONLY and not any_day and today.weekday() != 0:
        return WeeklyReportRunResult()
    advisor_rows = weekly_report_recipients(session)
    lead_rows = lead_report_recipients(session)
    if not advisor_rows and not lead_rows:
        return WeeklyReportRunResult()

    settings = settings or get_settings()
    mailer = mailer if mailer is not None else get_mailer(settings)
    week_start, week_end = report_week(today)
    week = f"{day_label(week_start)} to {day_label(week_end)}"
    reports = [
        report
        for report in build_weekly_reports(session, load_advisors(session), week_start, week_end)
        if report.book_clients > 0
    ]
    by_fa_id = {report.advisor.fa_id: report for report in reports}

    outbox: list[tuple[str, str, str, str, bytes]] = []
    for row in advisor_rows:
        report = by_fa_id.get(advisor_fa_id(row))
        if report is not None:
            outbox.append(
                (
                    KIND_ADVISOR,
                    row.email,
                    row.name or row.email,
                    f"Weekly follow-up report: {week}",
                    render_advisor_report(report),
                )
            )
    if reports and lead_rows:
        lead_pdf = render_lead_report(reports)
        for row in lead_rows:
            outbox.append(
                (
                    KIND_LEAD,
                    row.email,
                    row.name or row.email,
                    f"Advisor team weekly report: {week}",
                    lead_pdf,
                )
            )

    done = set() if resend else _already_sent(session, today)
    sent = already_sent = failed = 0
    for kind, email, name, subject, pdf in outbox:
        entity_id = f"{today.isoformat()}:{kind}:{email}"
        if entity_id in done:
            already_sent += 1
            continue
        detail = {"kind": kind, "week_start": week_start.isoformat(), "bytes": len(pdf)}
        try:
            to, cc = addressing(email, settings)
            mailer.send(
                EmailMessage(
                    to=to,
                    subject=subject,
                    text_body=(
                        f"Good morning {name},\n\n"
                        f"Your weekly follow-up report for {week} is attached.\n"
                    ),
                    cc=cc,
                    internal=True,
                    attachments=(
                        Attachment(
                            filename=f"weekly-report-{week_end.isoformat()}.pdf", content=pdf
                        ),
                    ),
                )
            )
            record_audit(
                session, entity_type=ENTITY_TYPE, action="send", entity_id=entity_id, detail=detail
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.exception("weekly_report.failed", kind=kind, recipient=email)
            record_audit(
                session,
                entity_type=ENTITY_TYPE,
                action="fail",
                entity_id=entity_id,
                detail={**detail, "error": str(exc)},
            )
        session.commit()

    close = getattr(mailer, "close", None)
    if close is not None:
        close()
    result = WeeklyReportRunResult(sent=sent, already_sent=already_sent, failed=failed)
    logger.info("weekly_report.completed", **result.__dict__)
    return result
