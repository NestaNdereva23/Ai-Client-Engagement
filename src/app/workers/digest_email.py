"""Send each account manager their morning email for one digest run.

Gathers the facts, hands them to briefing/digest_email.py to render, and
sends through the configured Mailer. The split is deliberate: everything
that reads or writes lives here, and the wording lives in a pure module
that can be tested without a database or a mail server.

The summary counts read risk_snapshot for the run, not the capped
digest_line rows, so a clipped list never understates the morning.

One advisor's failure never stops the rest: each is built, sent, and
committed on its own, and a failure audits and leaves no marker row, so the
advisor it failed for can be retried. A marker row for an advisor who has
already been mailed for this digest run is what stops a re-run sending
twice.

The email carries counts and money only, never a client name or a model
output, so this path touches no PII and can run the moment the risk run
commits -- it never has to wait on the narration warm-up. Advisor addresses
come from the environment and land in no table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.briefing.digest_email import (
    DigestEmailSummary,
    RenderedEmail,
    rank_signals,
    render_digest_email,
)
from app.config import FaRecord, Settings, get_settings
from app.db.models.digest import DigestEmailSend, DigestLine, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import RiskSnapshot
from app.delivery.mailer import EmailMessage, Mailer, get_mailer
from app.digest.build import DIGEST_ROUTES
from app.risk.history import previous_scores
from app.risk.signals import fired_signal_tags

logger = structlog.get_logger(__name__)

CALL_ROUTE = "fa_call_priority"


class DigestEmailAborted(RuntimeError):
    """Raised when there is no digest run to send emails for."""


@dataclass(frozen=True)
class DigestEmailRunResult:
    """What one pass over a digest run's advisors did."""

    digest_run_id: int
    advisors: int
    sent: int
    already_sent: int
    failed: int


@dataclass(frozen=True)
class AdvisorEmail:
    """One advisor's rendered email, before anything is sent."""

    fa_id: int
    advisor: FaRecord
    summary: DigestEmailSummary
    rendered: RenderedEmail


def _covering_from_digest(session: Session, digest_run_id: int) -> dict[int, int]:
    """Tonight's loans, read back off the digest.

    The nightly run already has the map in memory and passes it in. The
    command line script does not, so it recovers it from the lines that
    recorded one: group_key names whoever is calling, covering_for_fa_id
    names the owner they are calling for.
    """
    rows = session.execute(
        select(DigestLine.client_id, DigestLine.group_key).where(
            DigestLine.digest_run_id == digest_run_id,
            DigestLine.covering_for_fa_id.is_not(None),
        )
    ).all()
    covering: dict[int, int] = {}
    for row in rows:
        prefix, _, value = row.group_key.partition(":")
        if prefix == "fa" and value.isdigit():
            covering[row.client_id] = int(value)
    return covering


def _advisor_of(
    snapshot: RiskSnapshot, owners: dict[tuple[int, int], int], covering: dict[int, int]
) -> int | None:
    """Who is handling this client-fund tonight: the stand-in if their owner
    lent them out, otherwise the owner. The same choice the digest build
    made when it grouped the lines, so the email and the console never
    disagree.
    """
    owner = owners.get((snapshot.client_id, snapshot.unit_fund_id))
    if owner is None:
        return None
    return covering.get(snapshot.client_id, owner)


def _summaries(
    session: Session, risk_run_id: str, covering: dict[int, int]
) -> dict[int, DigestEmailSummary]:
    """One summary per advisor, counted over the run's whole snapshot
    population rather than the digest lines the per-group cap kept.
    """
    snapshots = list(
        session.scalars(
            select(RiskSnapshot).where(
                RiskSnapshot.run_id == risk_run_id, RiskSnapshot.route.in_(DIGEST_ROUTES)
            )
        )
    )
    if not snapshots:
        return {}

    client_ids = sorted({row.client_id for row in snapshots})
    owners = {
        (row.client_id, row.unit_fund_id): row.fa_id
        for row in session.execute(
            select(FaAssignment.client_id, FaAssignment.unit_fund_id, FaAssignment.fa_id).where(
                FaAssignment.client_id.in_(client_ids), FaAssignment.fa_id.is_not(None)
            )
        ).all()
    }
    prior = previous_scores(
        session, risk_run_id, [(row.client_id, row.unit_fund_id) for row in snapshots]
    )

    calls: dict[int, list[RiskSnapshot]] = {}
    watch: dict[int, list[RiskSnapshot]] = {}
    for row in snapshots:
        fa_id = _advisor_of(row, owners, covering)
        if fa_id is None:
            continue
        bucket = calls if row.route == CALL_ROUTE else watch
        bucket.setdefault(fa_id, []).append(row)

    summaries: dict[int, DigestEmailSummary] = {}
    for fa_id in sorted(set(calls) | set(watch)):
        call_rows = calls.get(fa_id, [])
        watch_rows = watch.get(fa_id, [])
        # A client with no earlier snapshot is new, which counts the same as
        # one whose score went up: both are things that were not on this
        # advisor's list yesterday in the state they are in today.
        escalated = sum(
            1
            for row in call_rows
            if row.risk_score > prior.get((row.client_id, row.unit_fund_id), -1)
        )
        summaries[fa_id] = DigestEmailSummary(
            clients_to_call=len({row.client_id for row in call_rows}),
            total_at_risk=sum(row.fund_at_risk for row in call_rows),
            new_or_escalated=escalated,
            watchlist_clients=len({row.client_id for row in watch_rows}),
            watchlist_at_risk=sum(row.fund_at_risk for row in watch_rows),
            signals=rank_signals([fired_signal_tags(row) for row in call_rows]),
        )
    return summaries


def _empty_summary() -> DigestEmailSummary:
    return DigestEmailSummary(
        clients_to_call=0,
        total_at_risk=0.0,
        new_or_escalated=0,
        watchlist_clients=0,
        watchlist_at_risk=0.0,
    )


def build_advisor_emails(
    session: Session,
    digest_run_id: int,
    *,
    roster: Sequence[FaRecord],
    covering: dict[int, int] | None = None,
    only_fa_id: int | None = None,
    console_base_url: str = "",
) -> list[AdvisorEmail]:
    """Render every rostered advisor's email for this digest run.

    An advisor with nothing to call is still rendered, with the short note
    saying so, so an inbox with no email in it always means a real failure.
    """
    digest_run = session.get(DigestRun, digest_run_id)
    if digest_run is None:
        raise DigestEmailAborted(f"no digest run {digest_run_id}")

    wanted = [record for record in roster if only_fa_id is None or record.fa_id == only_fa_id]
    if not wanted:
        return []

    if covering is None:
        covering = _covering_from_digest(session, digest_run_id)
    summaries = _summaries(session, digest_run.risk_run_id, covering)

    emails: list[AdvisorEmail] = []
    for record in wanted:
        summary = summaries.get(record.fa_id) or _empty_summary()
        emails.append(
            AdvisorEmail(
                fa_id=record.fa_id,
                advisor=record,
                summary=summary,
                rendered=render_digest_email(
                    fa_id=record.fa_id,
                    advisor_name=record.name,
                    summary=summary,
                    console_base_url=console_base_url,
                ),
            )
        )
    return emails


def _already_sent(session: Session, digest_run_id: int, fa_ids: Sequence[int]) -> set[int]:
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


def send_digest_emails(
    session: Session,
    digest_run_id: int,
    *,
    roster: Sequence[FaRecord] | None = None,
    mailer: Mailer | None = None,
    covering: dict[int, int] | None = None,
    only_fa_id: int | None = None,
    settings: Settings | None = None,
) -> DigestEmailRunResult:
    """Send one email per rostered advisor for this digest run.

    Each advisor is committed on their own, so an advisor whose send raises
    leaves the others already sent. A second call for the same digest run
    sends nothing: the marker rows written the first time are checked before
    anything goes out.
    """
    settings = settings or get_settings()
    roster = roster if roster is not None else settings.fa_records
    if not roster:
        return DigestEmailRunResult(
            digest_run_id=digest_run_id, advisors=0, sent=0, already_sent=0, failed=0
        )

    mailer = mailer if mailer is not None else get_mailer(settings)
    emails = build_advisor_emails(
        session,
        digest_run_id,
        roster=roster,
        covering=covering,
        only_fa_id=only_fa_id,
        console_base_url=settings.console_base_url,
    )
    done = _already_sent(session, digest_run_id, [email.fa_id for email in emails])

    sent = 0
    failed = 0
    for email in emails:
        if email.fa_id in done:
            continue
        try:
            result = mailer.send(
                EmailMessage(
                    to=email.advisor.email,
                    subject=email.rendered.subject,
                    text_body=email.rendered.text_body,
                )
            )
            status = "sent" if result.sent else "recorded"
            session.add(
                DigestEmailSend(
                    digest_run_id=digest_run_id,
                    fa_id=email.fa_id,
                    status=status,
                    client_count=email.summary.clients_to_call,
                    fund_value_total=email.summary.total_at_risk,
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
                    "clients": email.summary.clients_to_call,
                    "fund_value_total": email.summary.total_at_risk,
                },
            )
            session.commit()
            sent += 1
        except IntegrityError:
            # Something else mailed this advisor for this run between the
            # marker check and this write. Their email went out once, which
            # is exactly what the marker is for.
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
                    "clients": email.summary.clients_to_call,
                    "fund_value_total": email.summary.total_at_risk,
                    "error": str(exc),
                },
            )
            session.commit()

    result_summary = DigestEmailRunResult(
        digest_run_id=digest_run_id,
        advisors=len(emails),
        sent=sent,
        already_sent=len(done),
        failed=failed,
    )
    logger.info("digest_email.completed", **result_summary.__dict__)
    return result_summary
