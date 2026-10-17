from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.advisors import FaRecord
from app.config import Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.digest import DigestEmailSend, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures, RiskRun
from app.db.models.routing import ClientTier
from app.db.session import SessionLocal
from app.delivery.mailer import SendResult
from app.routing.tiers import (
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
    TIER_FREQUENT_DEPOSITOR,
    TIER_GRADUAL_DEPOSITOR,
)
from app.workers.digest_email import send_digest_emails

FUND_ID = 9531
CLIENTS = [95801, 95802, 95803, 95804]

ASHA = FaRecord(fa_id="fa-8801", name="Advisor Asha", email="asha@example.com", daily_capacity=19)
BRIAN = FaRecord(
    fa_id="fa-8802", name="Advisor Brian", email="brian@example.com", daily_capacity=19
)
ROSTER = (ASHA, BRIAN)


class FakeMailer:
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


def _settings(ticketing_base_url: str = "") -> Settings:
    return Settings(ticketing_base_url=ticketing_base_url)


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
            session.execute(delete(DigestRun).where(DigestRun.digest_run_id.in_(digest_run_ids)))
            session.execute(
                delete(AuditLog).where(
                    AuditLog.entity_type == "digest_email",
                    AuditLog.entity_id.in_(
                        [f"{d}:{r.fa_id}" for d in digest_run_ids for r in ROSTER]
                    ),
                )
            )
        session.execute(delete(RiskRun).where(RiskRun.run_id.in_(run_ids)))
        session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(CLIENTS)))
        session.execute(delete(ClientTier).where(ClientTier.client_id.in_(CLIENTS)))
        session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(CLIENTS)))
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENTS)))
        session.commit()


def _seed(cleanup, *, book: list[tuple[int, str, str, float, float]]) -> int:
    with SessionLocal() as session:
        run_id = uuid4().hex
        cleanup.append(run_id)
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        for client_id, fa_id, tier, balance, at_risk in book:
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    client_code=f"C{client_id}",
                    balance=balance,
                    n_deposits=1,
                    n_withdrawals=0,
                )
            )
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    balance_tier="Small",
                    sig_heavy_withdrawal=False,
                    sig_dormant=False,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=40,
                    risk_band="Watch",
                    risk_reasons="test",
                    fund_at_risk=at_risk,
                    config_version=1,
                    route="fa_action",
                    queue_rank=None,
                )
            )
            session.add(
                FaAssignment(
                    client_id=client_id, unit_fund_id=FUND_ID, fa_id=fa_id, source="roster"
                )
            )
            session.add(
                ClientTier(
                    client_id=client_id,
                    purchased_recently=False,
                    real_withdrawer=False,
                    tier=tier,
                    segment=SEGMENT_BY_TIER.get(tier),
                    computed_at=datetime.now(UTC),
                    thresholds_version=1,
                    run_id=run_id,
                )
            )
        digest_run = DigestRun(risk_run_id=run_id)
        session.add(digest_run)
        session.commit()
        return digest_run.digest_run_id


def _body_for(mailer: FakeMailer, email: str) -> str:
    return next(message.text_body for message in mailer.messages if message.to == email)


def _asha_book(cleanup) -> int:
    return _seed(
        cleanup,
        book=[
            (CLIENTS[0], ASHA.fa_id, TIER_GRADUAL_DEPOSITOR, 100_000.0, 30_000.0),
            (CLIENTS[1], ASHA.fa_id, TIER_GRADUAL_DEPOSITOR, 200_000.0, 50_000.0),
            (CLIENTS[2], ASHA.fa_id, TIER_FREQUENT_DEPOSITOR, 400_000.0, 20_000.0),
            (CLIENTS[3], BRIAN.fa_id, NO_TRANSACTIONS_IN_REVIEW_PERIOD, 10_000.0, 0.0),
        ],
    )


def test_the_email_says_what_happened_and_links_to_the_brief(db, cleanup) -> None:
    digest_run_id = _asha_book(cleanup)
    mailer = FakeMailer()

    with SessionLocal() as session:
        result = send_digest_emails(
            session,
            digest_run_id,
            roster=ROSTER,
            mailer=mailer,
            settings=_settings("http://ticketing.example"),
        )

    assert result.sent == 2
    body = _body_for(mailer, ASHA.email)
    assert "Good morning Advisor Asha," in body
    assert "Nothing new since yesterday." in body
    assert "By tier" not in body
    assert (
        "Open today's brief: http://ticketing.example/dashboard?view=ai-outreach&section=at-risk"
    ) in body
    subject = next(m.subject for m in mailer.messages if m.to == ASHA.email)
    assert subject == "Nothing new since yesterday"


def test_an_advisor_with_nothing_on_the_worklist_gets_the_short_note(db, cleanup) -> None:
    digest_run_id = _asha_book(cleanup)
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    body = _body_for(mailer, BRIAN.email)
    assert "Nothing on your worklist this morning." in body
    assert "By tier" not in body


def test_a_second_send_for_the_same_run_sends_nothing(db, cleanup) -> None:
    digest_run_id = _asha_book(cleanup)

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


def test_one_advisor_failing_leaves_the_others_sent_and_audits_the_failure(db, cleanup) -> None:
    digest_run_id = _asha_book(cleanup)
    mailer = FakeMailer(fail_for=ASHA.email)

    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    assert (result.sent, result.failed) == (1, 1)
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
    assert failure.detail["clients"] == 3
    assert failure.detail["fund_value_total"] == 100_000.0


def test_cc_only_copies_the_advisor_on_a_cytonn_address(db, cleanup) -> None:
    brian_cytonn = replace(BRIAN, email="brian@cytonn.com")
    digest_run_id = _asha_book(cleanup)
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session,
            digest_run_id,
            roster=(ASHA, brian_cytonn),
            mailer=mailer,
            settings=_settings(),
        )

    assert next(m for m in mailer.messages if m.to == ASHA.email).cc == ()
    assert next(m for m in mailer.messages if m.to == brian_cytonn.email).cc == (
        brian_cytonn.email,
    )


def test_an_advisor_with_no_clients_at_all_is_not_emailed(db, cleanup) -> None:
    digest_run_id = _seed(
        cleanup, book=[(CLIENTS[0], ASHA.fa_id, TIER_GRADUAL_DEPOSITOR, 100_000.0, 30_000.0)]
    )
    mailer = FakeMailer()

    with SessionLocal() as session:
        result = send_digest_emails(
            session, digest_run_id, roster=ROSTER, mailer=mailer, settings=_settings()
        )

    assert result.sent == 1
    assert [message.to for message in mailer.messages] == [ASHA.email]


def test_a_redirect_sends_every_digest_to_one_address_and_copies_nobody(db, cleanup) -> None:
    brian_cytonn = replace(BRIAN, email="brian@cytonn.com")
    digest_run_id = _asha_book(cleanup)
    mailer = FakeMailer()

    with SessionLocal() as session:
        send_digest_emails(
            session,
            digest_run_id,
            roster=(ASHA, brian_cytonn),
            mailer=mailer,
            settings=Settings(digest_redirect_to="qa@example.com"),
        )

    assert [(message.to, message.cc) for message in mailer.messages] == [
        ("qa@example.com", ()),
        ("qa@example.com", ()),
    ]
