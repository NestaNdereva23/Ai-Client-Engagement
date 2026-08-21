"""Tests for workers/digest_email.py: gathering, sending, and not re-sending.

Covers what the pure renderer cannot: the summary counts come from the run's
whole snapshot population rather than the capped digest lines, an advisor
already mailed for a digest run is skipped, one advisor failing leaves the
others sent, and this path never touches PiiVault or a model.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.config import FaRecord, Settings
from app.db.models.audit import AuditLog
from app.db.models.digest import DigestEmailSend, DigestLine, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.session import SessionLocal
from app.delivery.mailer import SendResult
from app.ingestion.fa_assignment_source import DbFaAssignmentSource
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult
from app.workers.digest import build_and_persist_digest
from app.workers.digest_email import send_digest_emails

FUND_ID = 9531
CLIENTS = [26421, 26422, 26423, 26424]

ASHA = FaRecord(fa_id="fa-8801", name="Advisor Asha", email="asha@example.com", daily_capacity=19)
BRIAN = FaRecord(
    fa_id="fa-8802", name="Advisor Brian", email="brian@example.com", daily_capacity=19
)
ROSTER = (ASHA, BRIAN)

SIGNALS_DORMANT = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}
SIGNALS_DORMANT_AND_SHRINKING = {**SIGNALS_DORMANT, "sig_shrinking": True}


class FakeMailer:
    """Records what it was handed and reports it as sent. Optionally raises
    for one advisor's address, so a single failure can be tested.
    """

    def __init__(self, fail_for: str | None = None) -> None:
        self.messages: list = []
        self.fail_for = fail_for

    def send(self, message):
        if self.fail_for is not None and message.to == self.fail_for:
            raise RuntimeError("smtp refused the connection")
        self.messages.append(message)
        return SendResult(
            sent=True, sender="ace@example.com", recipient=message.to, subject=message.subject
        )


def _settings(console_base_url: str = "") -> Settings:
    return Settings(console_base_url=console_base_url)


def _score(risk_score: int, fund_at_risk: float, signals: dict) -> ScoreResult:
    return ScoreResult(
        risk_score=risk_score,
        risk_band="Critical",
        risk_reasons="No deposit in 12 months",
        fund_at_risk=fund_at_risk,
        signals=signals,
        recency_band="1-2y",
        balance_tier="Large",
        value_tier="High",
    )


@pytest.fixture
def cleanup():
    run_ids: list[str] = []
    yield run_ids
    with SessionLocal() as session:
        digest_run_ids = list(
            session.scalars(
                select(DigestRun.digest_run_id).where(DigestRun.risk_run_id.in_(run_ids))
            )
        )
        if digest_run_ids:
            session.execute(
                delete(DigestEmailSend).where(DigestEmailSend.digest_run_id.in_(digest_run_ids))
            )
            session.execute(delete(DigestLine).where(DigestLine.digest_run_id.in_(digest_run_ids)))
            session.execute(delete(DigestRun).where(DigestRun.digest_run_id.in_(digest_run_ids)))
        session.execute(delete(RiskSnapshot).where(RiskSnapshot.run_id.in_(run_ids)))
        session.execute(delete(AuditLog).where(AuditLog.run_id.in_(run_ids)))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "digest_email",
                AuditLog.entity_id.in_([f"{d}:{r.fa_id}" for d in digest_run_ids for r in ROSTER]),
            )
        )
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(CLIENTS)))
        session.commit()


def _seed(
    cleanup,
    *,
    population: list[tuple[int, int, str, float, dict]],
    owners: dict[int, int],
    cap_per_group: int = 12,
    covering: dict[int, int] | None = None,
) -> int:
    """Build one digest run out of a population of (client, score, route,
    fund_at_risk, signals) rows, and return its digest_run_id.
    """
    with SessionLocal() as session:
        run_id = uuid4().hex
        cleanup.append(run_id)
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()

        for client_id, risk_score, route, fund_at_risk, signals in population:
            write_snapshot(
                session,
                run_id,
                client_id,
                FUND_ID,
                _score(risk_score, fund_at_risk, signals),
                RouteResult(
                    route=route,
                    queue_rank=1 if route == "fa_call_priority" else None,
                    complaint_caveat=False,
                ),
                config_version=1,
                pattern_is_reliable=True,
                overdue_multiple=1.0,
            )
        for client_id, fa_id in owners.items():
            session.add(
                FaAssignment(
                    client_id=client_id, unit_fund_id=FUND_ID, fa_id=fa_id, source="roster"
                )
            )
        session.flush()

        digest_run = build_and_persist_digest(
            session,
            run_id,
            fa_assignment_source=DbFaAssignmentSource(session=session),
            cap_per_group=cap_per_group,
            covering=covering,
        )
        session.commit()
        return digest_run.digest_run_id


def _body_for(mailer: FakeMailer, email: str) -> str:
    return next(message.text_body for message in mailer.messages if message.to == email)


def test_the_summary_counts_the_whole_population_not_the_capped_lines(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[
            (CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT),
            (CLIENTS[1], 80, "fa_call_priority", 2_000_000.0, SIGNALS_DORMANT),
            (CLIENTS[2], 70, "fa_call_priority", 1_000_000.0, SIGNALS_DORMANT),
        ],
        owners={client_id: ASHA.fa_id for client_id in CLIENTS[:3]},
        cap_per_group=2,
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    assert result.sent == 2
    body = _body_for(mailer, ASHA.email)
    assert "Clients to call today: 3" in body
    assert "Money at risk: KES 6,000,000" in body
    assert "Average at risk per client: KES 2,000,000" in body


def test_the_top_signal_comes_from_the_whole_population(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[
            (CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT_AND_SHRINKING),
            (CLIENTS[1], 80, "fa_call_priority", 2_000_000.0, SIGNALS_DORMANT),
            (CLIENTS[2], 70, "fa_call_priority", 1_000_000.0, SIGNALS_DORMANT),
        ],
        owners={client_id: ASHA.fa_id for client_id in CLIENTS[:3]},
        cap_per_group=1,
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    body = _body_for(mailer, ASHA.email)
    assert "Most common reason: No deposit in 12 months (3 clients)" in body
    assert "Next most common: Shrinking deposits (1 clients)" in body


def test_the_watchlist_is_counted_separately_from_the_call_queue(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[
            (CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT),
            (CLIENTS[1], 40, "fa_watchlist", 500_000.0, SIGNALS_DORMANT),
            (CLIENTS[2], 30, "fa_watchlist", 250_000.0, SIGNALS_DORMANT),
        ],
        owners={client_id: ASHA.fa_id for client_id in CLIENTS[:3]},
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session,
            digest_run_id,
            roster=ROSTER,
            mailer=mailer,
            settings=_settings("http://console.example"),
        )

    body = _body_for(mailer, ASHA.email)
    assert "Clients to call today: 1" in body
    assert (
        f"Watchlist: 2 clients, KES 750,000 (http://console.example/digest/fa:{ASHA.fa_id})" in body
    )


def test_a_second_send_for_the_same_run_and_advisor_is_refused(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
    )

    with SessionLocal() as session:
        first = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=FakeMailer(), settings=_settings()
        )

    second_mailer = FakeMailer()
    with SessionLocal() as session:
        second = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=second_mailer, settings=_settings()
        )

    assert first.sent == 2
    assert second.sent == 0
    assert second.already_sent == 2
    assert second_mailer.messages == []

    with SessionLocal() as session:
        markers = session.scalars(
            select(DigestEmailSend).where(DigestEmailSend.digest_run_id == digest_run_id)
        ).all()
    assert len(markers) == 2
    assert {marker.status for marker in markers} == {"sent"}


def test_one_advisor_failing_leaves_the_others_sent(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[
            (CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT),
            (CLIENTS[1], 80, "fa_call_priority", 2_000_000.0, SIGNALS_DORMANT),
        ],
        owners={CLIENTS[0]: ASHA.fa_id, CLIENTS[1]: BRIAN.fa_id},
    )
    mailer = FakeMailer(fail_for=ASHA.email)

    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    assert result.sent == 1
    assert result.failed == 1
    assert [message.to for message in mailer.messages] == [BRIAN.email]

    with SessionLocal() as session:
        markers = session.scalars(
            select(DigestEmailSend.fa_id).where(DigestEmailSend.digest_run_id == digest_run_id)
        ).all()
        failure = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_type == "digest_email",
                AuditLog.action == "fail",
                AuditLog.entity_id == f"{digest_run_id}:{ASHA.fa_id}",
            )
        )

    assert markers == [BRIAN.fa_id]
    assert failure is not None
    assert failure.detail["clients"] == 1
    assert failure.detail["fund_value_total"] == 3_000_000.0


def test_a_failed_advisor_can_be_retried(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
    )

    with SessionLocal() as session:
        send_digest_emails(
            session,
            digest_run_id,
            roster=ROSTER,
            mailer=FakeMailer(fail_for=ASHA.email),
            settings=_settings(),
        )

    retry = FakeMailer()
    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=retry, settings=_settings()
        )

    assert result.sent == 1
    assert [message.to for message in retry.messages] == [ASHA.email]


def test_an_advisor_with_nothing_to_call_gets_the_short_note(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    assert result.sent == 2
    body = _body_for(mailer, BRIAN.email)
    assert "Nothing on your call list this morning." in body
    assert "Clients to call today: 0" in body


def test_a_lent_client_is_counted_for_the_stand_in(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
        covering={CLIENTS[0]: BRIAN.fa_id},
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session,
            digest_run_id,
            roster=ROSTER,
            mailer=mailer,
            covering={CLIENTS[0]: BRIAN.fa_id},
            settings=_settings(),
        )

    brian_body = _body_for(mailer, BRIAN.email)
    asha_body = _body_for(mailer, ASHA.email)
    assert "Clients to call today: 1" in brian_body
    assert "Nothing on your call list this morning." in asha_body


def test_the_loan_is_recovered_from_the_digest_when_it_is_not_passed_in(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
        covering={CLIENTS[0]: BRIAN.fa_id},
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    assert "Clients to call today: 1" in _body_for(mailer, BRIAN.email)


def test_the_send_audits_the_advisor_the_count_and_the_value(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
    )

    with SessionLocal() as session:
        send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=FakeMailer(), settings=_settings()
        )

    with SessionLocal() as session:
        entry = session.scalar(
            select(AuditLog).where(
                AuditLog.entity_type == "digest_email",
                AuditLog.action == "send",
                AuditLog.entity_id == f"{digest_run_id}:{ASHA.fa_id}",
            )
        )

    assert entry is not None
    assert entry.detail["fa_id"] == ASHA.fa_id
    assert entry.detail["status"] == "sent"
    assert entry.detail["clients"] == 1
    assert entry.detail["fund_value_total"] == 3_000_000.0


def test_no_roster_sends_nothing(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=(), mailer=mailer, settings=_settings()
        )

    assert result.advisors == 0
    assert mailer.messages == []


def test_no_pii_vault_row_is_ever_read_on_this_path(db, cleanup, monkeypatch) -> None:
    """The email carries counts and money only, so this path never reads
    PiiVault or builds a model client. Every factory that could hand it a
    model client is replaced with one that raises, so a call to any of them
    fails the test rather than quietly working.
    """
    import app.privacy.llm_client as llm_client

    def _refuse(*args, **kwargs):
        raise AssertionError("the digest email path must never build a model client")

    monkeypatch.setattr(llm_client, "get_llm_client", _refuse)
    monkeypatch.setattr(llm_client, "get_briefing_llm_client", _refuse)

    digest_run_id = _seed(
        cleanup,
        population=[(CLIENTS[0], 90, "fa_call_priority", 3_000_000.0, SIGNALS_DORMANT)],
        owners={CLIENTS[0]: ASHA.fa_id},
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        before = session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "pii_vault")
        )
        send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    with SessionLocal() as session:
        after = session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "pii_vault")
        )

    assert after == before
