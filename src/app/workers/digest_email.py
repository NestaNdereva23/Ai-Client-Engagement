from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.advisors import FaRecord
from app.audit.log import record_audit
from app.briefing.book import BookFacts
from app.briefing.digest_email import RenderedEmail, render_digest_email
from app.config import Settings, get_settings
from app.db.models.digest import DigestEmailSend, DigestRun
from app.delivery.mailer import EmailMessage, Mailer, get_mailer, is_cytonn_address
from app.services.book_briefing import gather_book_facts, month_end_patterns
from app.services.client_touchpoints import today_in_nairobi
from app.services.user_access import digest_recipients

logger = structlog.get_logger(__name__)


class DigestEmailAborted(RuntimeError):
    pass


@dataclass(frozen=True)
class DigestEmailRunResult:
    digest_run_id: int
    advisors: int
    sent: int
    already_sent: int
    failed: int


@dataclass(frozen=True)
class AdvisorEmail:
    fa_id: str
    advisor: FaRecord
    facts: BookFacts
    rendered: RenderedEmail


def build_advisor_emails(
    session: Session,
    digest_run_id: int,
    *,
    roster: Sequence[FaRecord],
    only_fa_id: str | None = None,
    console_base_url: str = "",
    today: date | None = None,
) -> list[AdvisorEmail]:
    if session.get(DigestRun, digest_run_id) is None:
        raise DigestEmailAborted(f"no digest run {digest_run_id}")

    wanted = [record for record in roster if only_fa_id is None or record.fa_id == only_fa_id]
    if not wanted:
        return []

    patterns = month_end_patterns(
        session, [record.fa_id for record in wanted], today or today_in_nairobi()
    )
    emails: list[AdvisorEmail] = []
    for record in wanted:
        facts = gather_book_facts(
            session,
            record.fa_id,
            advisor_name=record.name,
            month_end=patterns.get(record.fa_id),
        )
        emails.append(
            AdvisorEmail(
                fa_id=record.fa_id,
                advisor=record,
                facts=facts,
                rendered=render_digest_email(
                    fa_id=record.fa_id, facts=facts, console_base_url=console_base_url
                ),
            )
        )
    return emails


def _already_sent(session: Session, digest_run_id: int, fa_ids: Sequence[str]) -> set[str]:
    if not fa_ids:
        return set()
    return set(
        session.scalars(
            select(DigestEmailSend.fa_id).where(
                DigestEmailSend.digest_run_id == digest_run_id,
                DigestEmailSend.fa_id.in_(list(fa_ids)),
            )
        )
    )


def _addressing(advisor_email: str, settings: Settings) -> tuple[str, tuple[str, ...]]:
    """Where one digest goes. With a redirect set, every digest goes to that one
    address and nobody is copied."""
    if settings.digest_redirect_to:
        return settings.digest_redirect_to, ()
    return advisor_email, ((advisor_email,) if is_cytonn_address(advisor_email) else ())


def send_digest_emails(
    session: Session,
    digest_run_id: int,
    *,
    roster: Sequence[FaRecord] | None = None,
    mailer: Mailer | None = None,
    only_fa_id: str | None = None,
    settings: Settings | None = None,
) -> DigestEmailRunResult:
    settings = settings or get_settings()
    if roster is None:
        roster = digest_recipients(session, settings)
    if not roster:
        return DigestEmailRunResult(
            digest_run_id=digest_run_id, advisors=0, sent=0, already_sent=0, failed=0
        )

    mailer = mailer if mailer is not None else get_mailer(settings)
    emails = build_advisor_emails(
        session,
        digest_run_id,
        roster=roster,
        only_fa_id=only_fa_id,
        console_base_url=settings.console_base_url,
    )
    emails = [email for email in emails if email.facts.assigned_clients > 0]
    done = _already_sent(session, digest_run_id, [email.fa_id for email in emails])

    sent = 0
    failed = 0
    for email in emails:
        if email.fa_id in done:
            continue
        try:
            to, cc = _addressing(email.advisor.email, settings)
            result = mailer.send(
                EmailMessage(
                    to=to,
                    subject=email.rendered.subject,
                    text_body=email.rendered.text_body,
                    cc=cc,
                )
            )
            status = "sent" if result.sent else "recorded"
            session.add(
                DigestEmailSend(
                    digest_run_id=digest_run_id,
                    fa_id=email.fa_id,
                    status=status,
                    client_count=email.facts.worklist_clients,
                    fund_value_total=email.facts.worklist_at_risk_kes,
                )
            )
            record_audit(
                session,
                entity_type="digest_email",
                action="send",
                entity_id=f"{digest_run_id}:{email.fa_id}",
                detail={
                    "digest_run_id": digest_run_id,
                    "fa_id": email.fa_id,
                    "status": status,
                    "clients": email.facts.worklist_clients,
                    "fund_value_total": email.facts.worklist_at_risk_kes,
                },
            )
            session.commit()
            sent += 1
        except IntegrityError:
            session.rollback()
            logger.info("digest_email.already_sent", digest_run_id=digest_run_id, fa_id=email.fa_id)
            done.add(email.fa_id)
        except Exception as exc:
            session.rollback()
            failed += 1
            logger.exception("digest_email.failed", digest_run_id=digest_run_id, fa_id=email.fa_id)
            record_audit(
                session,
                entity_type="digest_email",
                action="fail",
                entity_id=f"{digest_run_id}:{email.fa_id}",
                detail={
                    "digest_run_id": digest_run_id,
                    "fa_id": email.fa_id,
                    "clients": email.facts.worklist_clients,
                    "fund_value_total": email.facts.worklist_at_risk_kes,
                    "error": str(exc),
                },
            )
            session.commit()

    close = getattr(mailer, "close", None)
    if close is not None:
        close()

    result_summary = DigestEmailRunResult(
        digest_run_id=digest_run_id,
        advisors=len(emails),
        sent=sent,
        already_sent=len(done),
        failed=failed,
    )
    logger.info("digest_email.completed", **result_summary.__dict__)
    return result_summary
