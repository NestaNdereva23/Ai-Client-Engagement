from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.audit import AuditLog
from app.db.models.complaints import ClientComplaint
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.services.assistant_facts import build_assistant_facts
from app.services.briefing import gather_briefing_facts
from app.services.client_email_draft import EmailDraftBlocked
from app.services.client_touchpoints import today_in_nairobi
from app.services.email_options import ENTITY_TYPE, suggest_options_for_client

FUND = 9833
CLIENT = 983301
CLIENT_NAME = "Njeri Mwangi"
USER = "assistant-service-test"
REPLY = json.dumps(
    {
        "options": [
            {
                "title": "A warm thank you",
                "summary": "The email thanks the client for investing.",
                "why": "A steady investor with a large withdrawal this week.",
                "angle": "Write warmly and thank them for investing. Invite a short chat.",
                "tags": ["warm"],
                "recommended": True,
            },
            {
                "title": "A light check in",
                "summary": "The email says hello and asks one easy question.",
                "why": "Nobody has contacted the client lately.",
                "angle": "Keep it short and personal. Ask if there is anything to help with.",
                "tags": ["short"],
                "recommended": False,
            },
        ]
    }
)


class _ScriptedLLM:
    model = "assistant-service-stub"

    def __init__(self, reply: str = REPLY) -> None:
        self.reply = reply
        self.seen: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.seen.extend([system, user])
        return self.reply


def _clear(session) -> None:
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id == CLIENT)
    )
    session.execute(delete(ClientComplaint).where(ClientComplaint.client_id == CLIENT))
    session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id == CLIENT))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT))
    session.execute(delete(Clients).where(Clients.client_id == CLIENT))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND))
    session.execute(delete(AuditLog).where(AuditLog.actor_id == USER))
    session.commit()


@pytest.fixture
def seeded_client(db, monkeypatch):
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "true")
    get_settings.cache_clear()
    today = today_in_nairobi()
    with SessionLocal() as session:
        _clear(session)
        session.add(
            ActiveClientFund(
                client_id=CLIENT,
                unit_fund_id=FUND,
                client_code="C983301",
                balance=692_984.0,
                n_deposits=12,
                n_withdrawals=2,
                last_deposit_date=today - timedelta(days=25),
                typical_gap_days=67,
                largest_withdrawal=1_050_600.0,
                last_withdrawal_date=today - timedelta(days=3),
            )
        )
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT,
                unit_fund_id=FUND,
                balance_tier="Premium",
                overdue_multiple=None,
                sig_heavy_withdrawal=True,
                sig_dormant=False,
                sig_broken_pattern=False,
                sig_shrinking=False,
                sig_going_dormant=False,
                sig_never_repeated=False,
                risk_score=40,
                risk_band="Watch",
                risk_reasons="Large withdrawal",
                fund_at_risk=1_000.0,
                config_version=1,
                route="fa_action",
                queue_rank=1,
            )
        )
        session.add(PiiVault(client_id=CLIENT, client_name=CLIENT_NAME))
        session.commit()
    yield
    get_settings.cache_clear()
    with SessionLocal() as session:
        _clear(session)


def _facts_block(session):
    today = today_in_nairobi()
    facts = gather_briefing_facts(session, CLIENT, FUND, today)
    return build_assistant_facts(session, facts, CLIENT, FUND, today).to_dict()


def test_a_client_with_no_contact_and_a_large_recent_withdrawal(seeded_client) -> None:
    with SessionLocal() as session:
        block = _facts_block(session)

    assert block["largest_withdrawal_size_band"] == "large"
    assert block["last_withdrawal_recency_band"] == "this_week"
    assert block["deposit_rhythm_band"] == "every_two_months"
    assert block["last_contact_channel"] == "none"
    assert block["last_contact_age_band"] == "never"
    assert block["contacts_last_90_days_band"] == "none"


def test_logged_contacts_become_bands_and_the_note_is_never_read(seeded_client) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveClientInteraction(
                    client_id=CLIENT,
                    unit_fund_id=FUND,
                    type="email_sent",
                    reviewer_id=USER,
                    note="Called Njeri on 0712345678",
                ),
                ActiveClientInteraction(
                    client_id=CLIENT,
                    unit_fund_id=FUND,
                    type="call_logged",
                    outcome="connected",
                    reviewer_id=USER,
                    note="Asked about interest",
                ),
            ]
        )
        session.commit()
        block = _facts_block(session)

    assert block["last_contact_channel"] == "call"
    assert block["last_contact_outcome"] == "connected"
    assert block["last_contact_age_band"] == "this_week"
    assert block["contacts_last_90_days_band"] == "several"
    assert "0712345678" not in json.dumps(block)


def test_the_model_never_sees_the_client_and_the_crossing_is_audited(seeded_client) -> None:
    llm = _ScriptedLLM()

    with SessionLocal() as session:
        options = suggest_options_for_client(session, CLIENT, FUND, username=USER, llm_client=llm)

    sent = " ".join(llm.seen)
    for private in (CLIENT_NAME, "Njeri", "C983301", "692984", "692,984", str(CLIENT)):
        assert private not in sent
    assert [option.recommended for option in options] == [True, False]
    with SessionLocal() as session:
        rows = session.scalars(
            select(AuditLog).where(AuditLog.entity_type == ENTITY_TYPE, AuditLog.actor_id == USER)
        ).all()
    assert len(rows) == 1
    assert rows[0].detail["inbound"] == "pass"


def test_a_client_with_an_open_complaint_gets_no_options(seeded_client) -> None:
    with SessionLocal() as session:
        session.add(
            ClientComplaint(
                client_id=CLIENT,
                status="open",
                opened_at=today_in_nairobi(),
                category="service",
                channel="call",
            )
        )
        session.commit()
        with pytest.raises(EmailDraftBlocked):
            suggest_options_for_client(
                session, CLIENT, FUND, username=USER, llm_client=_ScriptedLLM()
            )
