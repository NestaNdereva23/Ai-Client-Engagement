from __future__ import annotations

import io
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfReader
from sqlalchemy import delete

from app.advisors import FaRecord
from app.briefing.weekly_report_pdf import render_lead_report
from app.config import Settings
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveTransaction,
)
from app.db.models.audit import AuditLog
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import RiskRun, RiskSnapshot
from app.db.models.user_access import ROLE_ADMIN, UserAccess
from app.db.session import SessionLocal
from app.delivery.mailer import SendResult
from app.services import weekly_report
from app.services.weekly_report import NOT_REACHED, REACHED, TRIED, build_weekly_reports
from app.workers.weekly_report import send_weekly_reports

FUND_ID = 9532
WITHDREW, CALLED, NO_ANSWER, QUIET = 95811, 95812, 95813, 95814
CLIENTS = [WITHDREW, CALLED, NO_ANSWER, QUIET]
TXN_IDS = [958110, 958120, 958140]
ASHA = FaRecord(
    fa_id="fa-8811", name="Advisor Asha", email="asha.weekly@example.com", daily_capacity=19
)
LEAD_EMAIL = "lead.weekly@example.com"
TODAY = date(2026, 11, 16)
WEEK = (date(2026, 11, 9), date(2026, 11, 15))
NAMES = {WITHDREW: "Joseph Kariuki", CALLED: "Mercy Achieng", NO_ANSWER: "Ruth Nyambura"}


class FakeMailer:
    def __init__(self) -> None:
        self.messages: list = []

    def send(self, message):
        self.messages.append(message)
        return SendResult(sent=True, sender="ace", recipient=message.to, subject=message.subject)


def _names(client_ids, **_):
    return {client_id: NAMES.get(client_id) for client_id in client_ids}


def _at(day: int, hour: int = 3) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


@pytest.fixture
def book(db, monkeypatch):
    monkeypatch.setattr(
        weekly_report,
        "load_active_config",
        lambda session, at: SimpleNamespace(at_risk_min=25, thresholds={"SYSTEM_FEE_MAX": 50.0}),
    )
    run_id = uuid4().hex
    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        for client_id, score in zip(CLIENTS, (80, 60, 55, 10), strict=True):
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    client_code=f"C{client_id}",
                    balance=1_000_000.0,
                    n_deposits=1,
                    n_withdrawals=0,
                )
            )
            session.add(FaAssignment(client_id=client_id, unit_fund_id=FUND_ID, fa_id=ASHA.fa_id))
            session.add(
                RiskSnapshot(
                    run_id=run_id,
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    sig_heavy_withdrawal=False,
                    sig_dormant=False,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=score,
                    risk_band="High",
                    risk_reasons="Deposits stopped",
                    fund_at_risk=10_000.0 * score,
                    config_version=1,
                    route="fa_call_priority",
                    created_at=_at(10),
                )
            )
        for client_id, outcome in ((CALLED, "connected"), (NO_ANSWER, "no_answer")):
            session.add(
                ActiveClientInteraction(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    type="call_logged",
                    outcome=outcome,
                    reviewer_id=ASHA.email,
                    created_at=_at(11, 9),
                )
            )
        for txn_id, client_id, txn_type, amount in zip(
            TXN_IDS,
            (WITHDREW, CALLED, QUIET),
            ("sale", "purchase", "purchase"),
            (500_000.0, 200_000.0, 50_000.0),
            strict=True,
        ):
            session.add(
                ActiveTransaction(
                    txn_id=txn_id,
                    txn_type=txn_type,
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    txn_date=date(2026, 11, 9),
                    amount=amount,
                )
            )
        session.commit()
    yield
    with SessionLocal() as session:
        for model in (RiskSnapshot, ActiveClientInteraction, FaAssignment, ActiveClientFund):
            session.execute(delete(model).where(model.client_id.in_(CLIENTS)))
        session.execute(delete(ActiveTransaction).where(ActiveTransaction.txn_id.in_(TXN_IDS)))
        session.execute(delete(RiskRun).where(RiskRun.run_id == run_id))
        session.execute(delete(UserAccess).where(UserAccess.email.in_([ASHA.email, LEAD_EMAIL])))
        session.execute(delete(AuditLog).where(AuditLog.entity_type == "weekly_report"))
        session.commit()


def _report():
    with SessionLocal() as session:
        return build_weekly_reports(session, [ASHA], *WEEK, client_names=_names)[0]


def test_flagged_clients_are_split_by_how_the_advisor_followed_up(book) -> None:
    report = _report()

    assert {client.client_id: client.status for client in report.flagged} == {
        WITHDREW: NOT_REACHED,
        CALLED: REACHED,
        NO_ANSWER: TRIED,
    }
    missed = report.not_reached[0]
    assert missed.name == "Joseph Kariuki"
    assert missed.reason == "Withdrew KSh 500K on 9 Nov"
    assert missed.on_record == "Nothing logged"
    assert report.new_flags == 3
    assert report.logged[0].total == 2


def test_money_is_split_between_reached_not_reached_and_the_rest(book) -> None:
    report = _report()

    assert report.money_reached.money_in == 200_000.0
    assert report.money_not_reached.money_out == 500_000.0
    assert report.money_rest.money_in == 50_000.0
    assert report.money_total.net == -250_000.0


def test_the_lead_pdf_has_the_team_page_and_each_advisor_sheet(book) -> None:
    pdf = PdfReader(io.BytesIO(render_lead_report([_report()])))
    text = "\n".join(page.extract_text() for page in pdf.pages)

    assert "Advisor team summary" in text
    assert "Withdrew, and nobody reached out" in text
    assert "No reach-out yet" in text
    assert "Joseph Kariuki" in text


def test_reports_go_to_the_people_switched_on_once(book, monkeypatch) -> None:
    monkeypatch.setattr(
        build_weekly_reports, "__kwdefaults__", {"client_names": _names, "compare": True}
    )
    with SessionLocal() as session:
        session.add(
            UserAccess(
                email=ASHA.email, name=ASHA.name, fa_id=ASHA.fa_id, receives_weekly_report=True
            )
        )
        session.add(UserAccess(email=LEAD_EMAIL, role=ROLE_ADMIN, receives_lead_report=True))
        session.commit()
    mailer = FakeMailer()

    with SessionLocal() as session:
        first = send_weekly_reports(session, today=TODAY, mailer=mailer, settings=Settings())
        second = send_weekly_reports(session, today=TODAY, mailer=mailer, settings=Settings())

    assert (first.sent, second.sent, second.already_sent) == (2, 0, 2)
    assert {message.to for message in mailer.messages} == {ASHA.email, LEAD_EMAIL}
    assert all(message.attachments[0].content.startswith(b"%PDF") for message in mailer.messages)
    assert all(message.internal for message in mailer.messages)
