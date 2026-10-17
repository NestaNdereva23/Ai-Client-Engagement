from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.api.routers.briefing as briefing_router
from app.briefing.client_email import DraftRejected, parse_email_draft
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund, ActiveClientTouchpoint
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment
from app.db.models.complaints import ClientComplaint
from app.db.models.fa_assignment import FaAssignment
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign, OutreachMessage
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.main import app
from app.services.agent_campaign_scope import message_is_visible
from app.services.client_touchpoints import today_in_nairobi

client = TestClient(app)

FUND = 9812
CLIENT = 981201
CLIENT_NAME = "Wanjiru Kamau"
CLIENT_CODE = "C981201"
URL = f"/api/v1/briefing/{CLIENT}/{FUND}/email-draft"
GOOD_REPLY = json.dumps(
    {
        "subject": "Checking in on your investment",
        "body": "Dear {{first_name}},\n\nIt has been a while. Would you like a short call?",
    }
)


class _ScriptedLLM:
    model = "email-draft-stub"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.seen.extend([system, user])
        return self.reply


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


@pytest.fixture
def model_on(monkeypatch):
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_model(monkeypatch, reply: str = GOOD_REPLY) -> _ScriptedLLM:
    llm = _ScriptedLLM(reply)
    monkeypatch.setattr(briefing_router, "get_briefing_llm_client", lambda settings: llm)
    return llm


def _ask(headers: dict | None = None):
    return client.post(URL, headers=headers)


def _clear(session) -> None:
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id == CLIENT)
    ).all()
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id == CLIENT))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id == CLIENT))
    session.execute(delete(Enrollment).where(Enrollment.client_id == CLIENT))
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "outreach_message", AuditLog.entity_id.in_(message_ids)
        )
    )
    session.execute(
        delete(Campaign).where(
            Campaign.campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE,
            Campaign.name == "Advisor requested email drafts",
            Campaign.campaign_id.not_in(select(OutreachMessage.campaign_id)),
        )
    )
    session.execute(delete(ClientComplaint).where(ClientComplaint.client_id == CLIENT))
    session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id == CLIENT))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT))
    session.execute(delete(Clients).where(Clients.client_id == CLIENT))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND))
    session.execute(
        delete(AuditLog).where(
            AuditLog.entity_type == "client_email_draft", AuditLog.actor_id.in_(["fa-1", "fa-2"])
        )
    )
    session.commit()


@pytest.fixture
def seeded_client(db):
    with SessionLocal() as session:
        _clear(session)
        session.add(
            ActiveClientFund(
                client_id=CLIENT,
                unit_fund_id=FUND,
                client_code=CLIENT_CODE,
                balance=1_234_567.0,
                n_deposits=3,
                n_withdrawals=0,
                last_deposit_date=date(2025, 1, 1),
            )
        )
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT,
                unit_fund_id=FUND,
                balance_tier="Premium",
                overdue_multiple=None,
                sig_heavy_withdrawal=False,
                sig_dormant=True,
                sig_broken_pattern=False,
                sig_shrinking=False,
                sig_going_dormant=False,
                sig_never_repeated=False,
                risk_score=60,
                risk_band="High",
                risk_reasons="No deposit in 12 months",
                fund_at_risk=1_000.0,
                config_version=1,
                route="fa_call_priority",
                queue_rank=1,
            )
        )
        session.add(PiiVault(client_id=CLIENT, client_name=CLIENT_NAME))
        session.commit()
    yield
    with SessionLocal() as session:
        _clear(session)


def test_it_says_so_plainly_when_the_model_is_off(monkeypatch, seeded_client) -> None:
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "false")
    get_settings.cache_clear()

    response = _ask()

    get_settings.cache_clear()
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "email_draft_disabled"


def test_a_draft_comes_back_for_review_and_the_model_never_sees_the_client(
    monkeypatch, model_on, seeded_client
) -> None:
    llm = _use_model(monkeypatch)

    response = _ask()

    assert response.status_code == 200
    body = response.json()
    assert body["drafts_left_today"] == get_settings().client_email_draft_daily_cap - 1
    sent_to_the_model = " ".join(llm.seen)
    for private in (CLIENT_NAME, "Wanjiru", CLIENT_CODE, "1234567", "1,234,567", str(CLIENT)):
        assert private not in sent_to_the_model


def test_a_missed_meeting_changes_why_the_email_is_written(
    monkeypatch, model_on, seeded_client
) -> None:
    llm = _use_model(monkeypatch)
    with SessionLocal() as session:
        session.add(
            ActiveClientTouchpoint(
                client_id=CLIENT,
                unit_fund_id=FUND,
                kind="meeting",
                title="Google Meet",
                due_date=today_in_nairobi() - timedelta(days=1),
                created_by="test",
            )
        )
        session.commit()

    try:
        response = _ask()
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ActiveClientTouchpoint).where(ActiveClientTouchpoint.client_id == CLIENT)
            )
            session.commit()

    assert response.status_code == 200
    assert "We missed a meeting that was planned" in " ".join(llm.seen)


def test_the_draft_waits_in_the_active_review_queue_and_not_the_dormant_one(
    monkeypatch, model_on, seeded_client
) -> None:
    _use_model(monkeypatch)

    message_id = _ask().json()["message_id"]

    with SessionLocal() as session:
        message = session.get(OutreachMessage, message_id)
        campaign_type = session.get(Campaign, message.campaign_id).campaign_type
    assert message.status == "pending_review"
    assert campaign_type == AGENT_PROPOSAL_CAMPAIGN_TYPE
    assert "{{" not in message.personalized_content["body"]
    assert "{{first_name}}" in message.ai_draft_content["body"]
    active_queue = client.get(
        "/api/v1/reviews",
        params={
            "campaign_type": AGENT_PROPOSAL_CAMPAIGN_TYPE,
            "order": "newest_first",
            "limit": 200,
        },
    ).json()
    dormant_queue = client.get(
        "/api/v1/reviews",
        params={"exclude_campaign_type": AGENT_PROPOSAL_CAMPAIGN_TYPE, "limit": 200},
    ).json()
    assert message_id in [item["message_id"] for item in active_queue["items"]]
    assert message_id not in [item["message_id"] for item in dormant_queue["items"]]


def test_the_advisor_who_owns_the_client_can_open_the_draft(
    monkeypatch, model_on, seeded_client
) -> None:
    _use_model(monkeypatch)
    with SessionLocal() as session:
        session.add(FaAssignment(client_id=CLIENT, unit_fund_id=FUND, fa_id="fa-owner"))
        session.commit()

    try:
        message_id = _ask().json()["message_id"]
        with SessionLocal() as session:
            owner_sees_it = message_is_visible(session, message_id, "fa-owner")
            stranger_sees_it = message_is_visible(session, message_id, "fa-stranger")
    finally:
        with SessionLocal() as session:
            session.execute(delete(Enrollment).where(Enrollment.client_id == CLIENT))
            session.execute(delete(FaAssignment).where(FaAssignment.client_id == CLIENT))
            session.commit()

    assert owner_sees_it
    assert not stranger_sees_it


def test_the_daily_cap_stops_further_drafts(
    monkeypatch, model_on, seeded_client, reviewer_2_headers
) -> None:
    monkeypatch.setenv("CLIENT_EMAIL_DRAFT_DAILY_CAP", "1")
    get_settings.cache_clear()
    _use_model(monkeypatch)

    first = _ask()
    second = _ask()
    other_advisor = _ask(reviewer_2_headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "email_draft_limit_reached"
    assert other_advisor.status_code == 200


def test_a_client_with_an_open_complaint_gets_no_draft(
    monkeypatch, model_on, seeded_client
) -> None:
    llm = _use_model(monkeypatch)
    with SessionLocal() as session:
        session.add(
            ClientComplaint(
                client_id=CLIENT,
                opened_at=date(2026, 10, 1),
                status="open",
                category="billing",
                channel="email",
            )
        )
        session.commit()

    response = _ask()

    assert response.status_code == 409
    assert llm.seen == []


def test_a_reply_with_numbers_is_not_passed_on(monkeypatch, model_on, seeded_client) -> None:
    reply = json.dumps({"subject": "Hello", "body": "Dear {{first_name}}, your 12% is safe."})
    _use_model(monkeypatch, reply)

    response = _ask()

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "email_draft_failed"


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        json.dumps({"subject": "Hi", "body": "We hope you are well."}),
        json.dumps({"subject": "", "body": "Dear {{first_name}}, hello"}),
        json.dumps({"subject": "Hi", "body": "Dear {{first_name}}, {{typical_contribution}}"}),
    ],
)
def test_a_malformed_draft_is_rejected(reply) -> None:
    with pytest.raises(DraftRejected):
        parse_email_draft(reply)


@pytest.mark.parametrize("greeting", ["Dear [Client Name],", "Dear Wanjiru,", "Hi Client,"])
def test_a_greeting_without_the_placeholder_is_repaired(greeting) -> None:
    reply = json.dumps({"subject": "Hello", "body": f"{greeting}\nHow are you?"})

    draft = parse_email_draft(reply)

    assert draft.body.startswith("Dear {{first_name}},")
    assert "Wanjiru" not in draft.body


@pytest.mark.parametrize("token", ["{first_name}", "[first_name]", "{{ first_name }}"])
def test_a_loosely_written_placeholder_is_corrected(token) -> None:
    reply = json.dumps({"subject": "Hello", "body": f"Dear {token}, how are you?"})

    draft = parse_email_draft(reply)

    assert draft.body.startswith("Dear {{first_name}},")


def test_a_draft_wrapped_in_a_code_fence_is_still_read() -> None:
    draft = parse_email_draft(f"```json\n{GOOD_REPLY}\n```")

    assert draft.subject == "Checking in on your investment"
