from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.agents.client_assistant as assistant_module
import app.api.routers.briefing as briefing_router
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.agent_event import AgentEvent
from app.db.models.agent_prompt import AgentPrompt
from app.db.models.agent_run import AgentRun
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment
from app.db.models.chat import ChatSession, ChatTurn
from app.db.models.complaints import ClientComplaint
from app.db.models.fa_assignment import FaAssignment
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import AGENT_PROPOSAL_CAMPAIGN_TYPE, Campaign, OutreachMessage
from app.db.models.risk import ClientRiskFeatures
from app.db.models.user_access import ROLE_ADVISOR, UserAccess
from app.db.session import SessionLocal
from app.main import app
from app.services.client_touchpoints import today_in_nairobi

client = TestClient(app)

FUND = 9834
CLIENT = 983401
OTHER_CLIENT = 983402
OWNER = "assistant-owner@example.com"
OWNER_FA = "fa-assistant-owner"
STRANGER = "assistant-stranger@example.com"
BASE = f"/api/v1/briefing/{CLIENT}/{FUND}/assistant"

OPTIONS_REPLY = json.dumps(
    {
        "options": [
            {
                "title": "A light check in",
                "summary": "The email says hello and asks one easy question.",
                "why": "Nobody has contacted the client lately.",
                "angle": "Keep it short and personal. Ask if there is anything to help with.",
                "tags": ["short"],
                "recommended": False,
            },
            {
                "title": "A warm thank you",
                "summary": "The email thanks the client for investing.",
                "why": "A steady investor with a large withdrawal this week.",
                "angle": "Write warmly and thank them for investing. Invite a short chat.",
                "tags": ["warm"],
                "recommended": True,
            },
        ]
    }
)
DRAFT_REPLY = json.dumps(
    {"subject": "Thank you", "body": "Dear {{first_name}},\n\nThank you. Shall we talk?"}
)


class _Stub:
    model = "assistant-stub"

    def __init__(self) -> None:
        self.systems: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.systems.append(system)
        if "decide which email to send" in system:
            return OPTIONS_REPLY
        if "Write one short email" in system:
            return DRAFT_REPLY
        return "A short narrative about this client."


@pytest.fixture
def stub(monkeypatch) -> _Stub:
    llm = _Stub()
    monkeypatch.setattr(assistant_module, "get_briefing_llm_client", lambda settings: llm)
    monkeypatch.setattr(briefing_router, "get_briefing_llm_client", lambda settings: llm)
    return llm


def _clear(session) -> None:
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id == CLIENT)
    )
    turn_run_ids = session.scalars(
        select(ChatTurn.run_id)
        .join(ChatSession, ChatSession.session_id == ChatTurn.session_id)
        .where(ChatSession.client_id.in_([CLIENT, OTHER_CLIENT]))
    ).all()
    session.execute(
        delete(ChatTurn).where(
            ChatTurn.session_id.in_(
                select(ChatSession.session_id).where(
                    ChatSession.client_id.in_([CLIENT, OTHER_CLIENT])
                )
            )
        )
    )
    session.execute(delete(ChatSession).where(ChatSession.client_id.in_([CLIENT, OTHER_CLIENT])))
    session.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(turn_run_ids)))
    session.execute(delete(AgentRun).where(AgentRun.run_id.in_(turn_run_ids)))
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
    session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_([CLIENT, OTHER_CLIENT])))
    session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id == CLIENT))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT))
    session.execute(delete(Clients).where(Clients.client_id == CLIENT))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND))
    session.execute(
        delete(AuditLog).where(
            AuditLog.actor_id.in_([OWNER, STRANGER]),
            AuditLog.entity_type.in_(["client_email_draft", "client_email_options"]),
        )
    )
    session.execute(delete(UserAccess).where(UserAccess.email.in_([OWNER, STRANGER])))
    session.commit()


@pytest.fixture
def seeded(db, configured_reviewers, monkeypatch):
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "true")
    monkeypatch.setenv("AGENT_FA_SCOPED", "true")
    get_settings.cache_clear()
    today = today_in_nairobi()
    with SessionLocal() as session:
        _clear(session)
        session.add_all(
            [
                UserAccess(email=OWNER, role=ROLE_ADVISOR, fa_id=OWNER_FA, active=True),
                UserAccess(email=STRANGER, role=ROLE_ADVISOR, fa_id="fa-stranger", active=True),
                FaAssignment(client_id=CLIENT, unit_fund_id=FUND, fa_id=OWNER_FA),
                ActiveClientFund(
                    client_id=CLIENT,
                    unit_fund_id=FUND,
                    client_code="C983401",
                    balance=692_984.0,
                    n_deposits=12,
                    n_withdrawals=2,
                    last_deposit_date=today - timedelta(days=25),
                    typical_gap_days=67,
                    largest_withdrawal=1_050_600.0,
                    last_withdrawal_date=today - timedelta(days=3),
                ),
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
                ),
                PiiVault(client_id=CLIENT, client_name="Njeri Mwangi"),
            ]
        )
        session.commit()
    yield
    get_settings.cache_clear()
    with SessionLocal() as session:
        _clear(session)


def _post(headers, **body):
    return client.post(f"{BASE}/turns", json=body, headers=headers)


def _session_of(headers, session_id: int) -> dict:
    return client.get(f"{BASE}/sessions/{session_id}", headers=headers).json()


def _options_turn(headers) -> dict:
    posted = _post(headers, starter_code="email_options")
    assert posted.status_code == 202
    return _session_of(headers, posted.json()["session_id"])


def _types(turn: dict) -> list[str]:
    return [block["type"] for block in turn["blocks"]]


def test_starters_come_from_the_backend_and_say_when_one_is_off(
    seeded, headers_for, monkeypatch
) -> None:
    on = client.get(f"{BASE}/starters", headers=headers_for(OWNER)).json()
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "false")
    get_settings.cache_clear()
    off = client.get(f"{BASE}/starters", headers=headers_for(OWNER)).json()

    assert [starter["code"] for starter in on] == ["quick_email", "email_options", "deeper_brief"]
    assert all(starter["enabled"] for starter in on)
    by_code = {starter["code"]: starter for starter in off}
    assert by_code["email_options"]["enabled"] is False
    assert by_code["quick_email"]["enabled"] is False
    assert by_code["email_options"]["disabled_reason"]
    assert by_code["deeper_brief"]["enabled"] is True


def test_the_about_strip_comes_from_the_backend(seeded, headers_for) -> None:
    headers = headers_for(OWNER)
    with SessionLocal() as session:
        session.add(
            ActiveClientInteraction(
                client_id=CLIENT, unit_fund_id=FUND, type="call_logged", reviewer_id=OWNER
            )
        )
        session.commit()

    body = client.get(f"{BASE}/about", headers=headers).json()
    refused = client.get(f"{BASE}/about", headers=headers_for(STRANGER))

    assert body["reads"] == ["Verified brief", "Past interactions (1)", "Deposits and withdrawals"]
    assert "not the client's name" in body["footnote"]
    assert (body["title"], body["reads_label"]) == ("Client copilot", "Using")
    assert body["greeting"]
    assert refused.status_code == 403


def test_asking_for_email_options_streams_steps_and_stores_blocks(
    seeded, headers_for, stub
) -> None:
    headers = headers_for(OWNER)

    session_body = _options_turn(headers)

    turn = session_body["turns"][0]
    assert turn["run_state"] == "completed"
    assert _types(turn) == ["steps", "text", "options"]
    options = turn["blocks"][2]["items"]
    assert [item["recommended"] for item in options] == [True, False]
    for item in options:
        assert item["action"]["token"].startswith("draft_email:")
        assert not any(key.startswith("_") for key in item)
    assert "angle" not in json.dumps(turn["blocks"])
    events = client.get(f"/api/v1/agent/runs/{turn['run_id']}/events", headers=headers).json()
    kinds = [event["kind"] for event in events["events"]]
    assert "step_started" in kinds and "step_completed" in kinds
    stages = [e["detail"].get("stage") for e in events["events"] if e["kind"] == "run_status"]
    assert stages.count("block_ready") == 3
    assert kinds[-1] == "run_completed"


def test_picking_an_option_writes_a_draft_from_its_angle_and_counts_one_draft(
    seeded, headers_for, stub, monkeypatch
) -> None:
    monkeypatch.setenv("CLIENT_EMAIL_DRAFT_DAILY_CAP", "1")
    get_settings.cache_clear()
    headers = headers_for(OWNER)
    first = _options_turn(headers)
    again = _options_turn(headers)
    token = first["turns"][0]["blocks"][2]["items"][0]["action"]["token"]

    posted = _post(headers, session_id=first["session_id"], action=token)
    second_pick = _post(
        headers,
        session_id=again["session_id"],
        action=again["turns"][0]["blocks"][2]["items"][0]["action"]["token"],
    )

    assert posted.status_code == 202
    turn = _session_of(headers, first["session_id"])["turns"][1]
    assert turn["question"] == 'Write the "A warm thank you" email'
    assert _types(turn) == ["steps", "result"]
    result = turn["blocks"][1]
    assert result["next"]["kind"] == "review_message"
    with SessionLocal() as session:
        message = session.get(OutreachMessage, result["next"]["ref"])
        assert message.status == "pending_review"
    assert any("Write warmly and thank them for investing" in system for system in stub.systems)
    blocked = _session_of(headers, again["session_id"])["turns"][1]
    assert second_pick.status_code == 202
    assert _types(blocked) == ["notice"]
    assert "today's email drafts" in blocked["blocks"][0]["text"]


def test_a_quick_email_picks_the_top_option_and_drafts_it(seeded, headers_for, stub) -> None:
    headers = headers_for(OWNER)
    posted = _post(headers, starter_code="quick_email").json()

    turn = _session_of(headers, posted["session_id"])["turns"][0]

    assert turn["run_state"] == "completed"
    assert _types(turn) == ["steps", "text", "result"]
    assert [item["label"] for item in turn["blocks"][0]["items"]] == [
        "Reading the verified brief",
        "Checking complaints and do not contact",
        "Reading past interactions",
        "Choosing the kinds of email that fit",
        "Writing the draft",
        "Saving it to the Review Queue",
    ]
    assert "A warm thank you" in turn["blocks"][1]["text"]
    assert turn["blocks"][2]["next"]["kind"] == "review_message"
    assert any("Write warmly and thank them for investing" in system for system in stub.systems)


def test_the_draft_endpoint_takes_a_chosen_option(seeded, headers_for, stub) -> None:
    headers = headers_for(OWNER)
    body = _options_turn(headers)
    turn = body["turns"][0]
    option_id = turn["blocks"][2]["items"][1]["id"]

    drafted = client.post(
        f"/api/v1/briefing/{CLIENT}/{FUND}/email-draft",
        json={"turn_id": turn["turn_id"], "option_id": option_id},
        headers=headers,
    )
    missing = client.post(
        f"/api/v1/briefing/{CLIENT}/{FUND}/email-draft",
        json={"turn_id": turn["turn_id"], "option_id": "nope"},
        headers=headers,
    )

    assert drafted.status_code == 200
    assert "message_id" in drafted.json()
    assert any("Keep it short and personal" in system for system in stub.systems)
    assert missing.status_code == 404


def test_an_advisor_outside_the_book_sees_nothing(seeded, headers_for, stub) -> None:
    owner = headers_for(OWNER)
    stranger = headers_for(STRANGER)
    body = _options_turn(owner)
    run_id = body["turns"][0]["run_id"]

    starters = client.get(f"{BASE}/starters", headers=stranger)
    read = client.get(f"{BASE}/sessions/{body['session_id']}", headers=stranger)
    ask = _post(stranger, starter_code="deeper_brief")
    own_events = client.get(f"/api/v1/agent/runs/{run_id}/events", headers=owner)
    their_events = client.get(f"/api/v1/agent/runs/{run_id}/events", headers=stranger)

    assert (starters.status_code, read.status_code, ask.status_code) == (403, 403, 403)
    assert own_events.status_code == 200
    assert their_events.status_code == 404


def test_a_session_belongs_to_its_client_and_its_owner(seeded, headers_for, stub) -> None:
    headers = headers_for(OWNER)
    body = _options_turn(headers)
    with SessionLocal() as session:
        session.add(FaAssignment(client_id=OTHER_CLIENT, unit_fund_id=FUND, fa_id=OWNER_FA))
        session.commit()
    other_base = f"/api/v1/briefing/{OTHER_CLIENT}/{FUND}/assistant"

    read = client.get(f"{other_base}/sessions/{body['session_id']}", headers=headers)
    post = client.post(
        f"{other_base}/turns",
        json={"session_id": body["session_id"], "text": "hello"},
        headers=headers,
    )
    listed = client.get(f"{BASE}/sessions", headers=headers).json()

    assert read.status_code == 404
    assert post.status_code == 404
    assert [row["session_id"] for row in listed] == [body["session_id"]]
    assert listed[0]["turn_count"] == 1


def test_a_made_up_or_foreign_action_is_refused(seeded, headers_for, stub) -> None:
    headers = headers_for(OWNER)
    body = _options_turn(headers)
    turn_id = body["turns"][0]["turn_id"]

    unknown_option = _post(headers, action=f"draft_email:{turn_id}:nope")
    unknown_turn = _post(headers, action="draft_email:999999999:abc")
    garbage = _post(headers, action="send_everything:1:2")

    assert unknown_option.status_code == 422
    assert unknown_turn.status_code == 422
    assert garbage.status_code == 422


def test_a_request_must_carry_exactly_one_thing(seeded, headers_for) -> None:
    headers = headers_for(OWNER)

    assert _post(headers).status_code == 422
    assert _post(headers, text="hi", starter_code="email_options").status_code == 422
    assert _post(headers, starter_code="not_a_starter").status_code == 404


def test_typed_text_gets_the_starters_back(seeded, headers_for) -> None:
    headers = headers_for(OWNER)
    posted = _post(headers, text="what now?").json()

    turn = _session_of(headers, posted["session_id"])["turns"][0]

    assert _types(turn) == ["text", "suggestions"]
    labels = [item["label"] for item in turn["blocks"][1]["items"]]
    assert labels == ["Draft a quick email", "Compare email options", "Gather a deeper brief"]
    assert turn["blocks"][1]["items"][0]["starter_code"] == "quick_email"


def test_the_deeper_brief_comes_back_as_blocks(seeded, headers_for, stub) -> None:
    headers = headers_for(OWNER)
    posted = _post(headers, starter_code="deeper_brief").json()

    turn = _session_of(headers, posted["session_id"])["turns"][0]

    assert turn["run_state"] == "completed"
    assert _types(turn)[:2] == ["steps", "text"]
    assert turn["blocks"][1]["title"] == "Deeper brief"
    assert "suggestions" in _types(turn)
    suggested = [item["starter_code"] for item in turn["blocks"][-1]["items"]]
    assert "deeper_brief" not in suggested


def test_a_client_with_an_open_complaint_gets_a_notice_not_options(
    seeded, headers_for, stub
) -> None:
    headers = headers_for(OWNER)
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

    starters = client.get(f"{BASE}/starters", headers=headers).json()
    refused = _post(headers, starter_code="email_options")

    email = next(starter for starter in starters if starter["code"] == "email_options")
    assert email["enabled"] is False
    assert refused.status_code == 409
    assert stub.systems == []


def test_the_ask_screen_never_lists_a_client_session(seeded, headers_for, stub) -> None:
    headers = headers_for(OWNER)
    body = _options_turn(headers)

    listed = client.get("/api/v1/agent/chat/sessions", headers=headers).json()
    fetched = client.get(f"/api/v1/agent/chat/sessions/{body['session_id']}", headers=headers)
    posted = client.post(
        "/api/v1/agent/chat",
        json={"question": "hello", "session_id": body["session_id"]},
        headers=headers,
    )

    assert body["session_id"] not in [row["session_id"] for row in listed["items"]]
    assert fetched.status_code == 404
    assert posted.status_code == 404


@pytest.fixture
def studio_prompt():
    added: list[tuple[str, int]] = []

    def publish(name: str, template: str) -> None:
        version = 9000 + len(added)
        with SessionLocal() as session:
            session.add(
                AgentPrompt(
                    version=version,
                    prompt_name=name,
                    template=template,
                    valid_from=today_in_nairobi(),
                    status="published",
                )
            )
            session.commit()
        added.append((name, version))

    yield publish
    with SessionLocal() as session:
        for name, version in added:
            session.execute(
                delete(AgentPrompt).where(
                    AgentPrompt.prompt_name == name, AgentPrompt.version == version
                )
            )
        session.commit()


def test_a_studio_edit_changes_what_the_options_model_is_told(
    seeded, headers_for, stub, studio_prompt
) -> None:
    studio_prompt(
        "email_options_suggest",
        "STUDIO-OPTIONS-MARKER. You help a financial advisor decide which email to send.",
    )

    _options_turn(headers_for(OWNER))

    assert any("STUDIO-OPTIONS-MARKER" in system for system in stub.systems)


def test_a_studio_edit_changes_what_the_email_writer_is_told(
    seeded, headers_for, stub, studio_prompt
) -> None:
    studio_prompt("client_email_draft", "STUDIO-DRAFT-MARKER. Write one short email to the client.")
    headers = headers_for(OWNER)
    body = _options_turn(headers)
    token = body["turns"][0]["blocks"][2]["items"][0]["action"]["token"]

    _post(headers, session_id=body["session_id"], action=token)

    writer = [system for system in stub.systems if "Write one short email" in system]
    assert writer and all("STUDIO-DRAFT-MARKER" in system for system in writer)
