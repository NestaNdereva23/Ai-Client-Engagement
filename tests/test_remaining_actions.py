from __future__ import annotations

import functools
import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from app.agents.action_catalog import load_active_actions
from app.agents.email_agent import build_system_prompt
from app.agents.email_channel import EmailAgent
from app.agents.graph import load_client_context
from app.agents.guide_mix import MIX_INSTRUCTIONS, guide_brief_for_campaign
from app.agents.orchestrator import Orchestrator
from app.agents.permissions import effective_permission, set_permission
from app.agents.proposal_state import transition_proposal
from app.agents.propose import (
    ALREADY_OFFERED,
    NO_APPROVED_GUIDE,
    group_skip_reasons,
    member_skip_reason,
)
from app.agents.situation_action_mapping import load_active_mappings
from app.agents.watchlist import GroupMember
from app.agents.write_tools import RUN_PROPOSAL, WRITE_PROPOSAL, make_write_tools
from app.campaigns.generation import generate_for_enrollment
from app.campaigns.touch import SendResult, run_due_enrollments, send_due_touches
from app.config import Settings
from app.db.models.active_clients import (
    FLAGGED_FOR_ACCOUNT_MANAGER,
    ActiveClientFund,
    ActiveClientInteraction,
)
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.campaigns import CampaignStep as Step
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.rag import DOC_TYPE_CLIENT_GUIDE, RagChunk, RagDocument, RagDocumentVersion
from app.db.models.risk import ClientRiskFeatures
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.rag.embedding import HashingEmbedder
from app.rag.guides import add_guide, approve_guide_version
from app.rules.catalog import load_active_angles
from app.rules.tier_contract import load_tier
from app.services.review import decide

FUND_ID = 9748
CLIENT_ID = 974801
BALANCE = 7_777.0
TIER = "one_time_withdrawers"
TODAY = date.today()

INSIGHT_TITLE = "A group the remaining actions were run against"
GUIDE_TITLE = "Remaining actions test: why some investors hold more than one fund"
GUIDE_TEXT = (
    "Different funds are built for different jobs, and knowing what each one is for "
    "makes it easier to choose."
)
GROUP_FILTER = {
    "conditions": [
        {"field": "balance", "op": "gt", "value": BALANCE - 0.5},
        {"field": "balance", "op": "lt", "value": BALANCE + 0.5},
    ]
}

FOLLOW_UP = "follow_up_when_no_one_called"
SECOND_FUND = "suggest_second_fund"
ACTIONS = (FOLLOW_UP, SECOND_FUND, "ask_what_changed", "send_learning_note", "start_win_back")
NEEDS_A_GUIDE = (SECOND_FUND, "send_learning_note")
SITUATION_ANGLES = {
    "waiting_on_a_call": FOLLOW_UP,
    "healthy_one_fund": SECOND_FUND,
    "getting_smaller": "ask_what_changed",
    "very_small_and_quiet": "start_win_back",
}

EMB = HashingEmbedder()
SETTINGS = Settings(
    llm_provider="anthropic",
    anthropic_api_key="test-key",
    llm_model="claude-opus-5",
    llm_temperature=None,
    llm_max_tokens=1024,
)


class ScriptedModel:
    model = "scripted"

    def __init__(self, sign_off: str) -> None:
        self.sign_off = sign_off
        self.systems: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.systems.append(system)
        body = (
            "Hi {{first_name}}, we are writing about your {{fund_name}} account. "
            f"Reply to this email if you would like to talk it through.\n{self.sign_off}"
        )
        return json.dumps({"subject": "A note from Cytonn", "body": body})


def _purge(session) -> None:
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id == CLIENT_ID)
    ).all()
    if message_ids:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
    enrollment_ids = session.scalars(
        select(Enrollment.enrollment_id).where(Enrollment.client_id == CLIENT_ID)
    ).all()
    if enrollment_ids:
        session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id == CLIENT_ID))
    session.execute(delete(Enrollment).where(Enrollment.client_id == CLIENT_ID))

    run_ids = session.scalars(
        select(GenerationRun.run_id).where(GenerationRun.client_id == CLIENT_ID)
    ).all()
    if run_ids:
        request_ids = session.scalars(
            select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))
        ).all()
        if request_ids:
            session.execute(delete(TokenUsage).where(TokenUsage.request_id.in_(request_ids)))
            session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
            session.execute(delete(LLMRequest).where(LLMRequest.run_id.in_(run_ids)))
        session.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
        session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
        session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))

    insight_ids = session.scalars(
        select(AgentInsight.insight_id).where(AgentInsight.title == INSIGHT_TITLE)
    ).all()
    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id).where(AgentProposal.insight_id.in_(insight_ids or [0]))
    ).all()
    if proposal_ids:
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids))
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(proposal_ids)))
    if insight_ids:
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))

    campaign_ids = session.scalars(
        select(Campaign.campaign_id).where(Campaign.campaign_type == "agent_proposal")
    ).all()
    if campaign_ids:
        session.execute(delete(Step).where(Step.campaign_id.in_(campaign_ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))

    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id == CLIENT_ID)
    )
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT_ID))
    session.execute(
        delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == CLIENT_ID)
    )
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == CLIENT_ID))
    session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
    session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.execute(delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id == CLIENT_ID))

    doc_ids = session.scalars(
        select(RagDocument.doc_id).where(
            RagDocument.doc_type == DOC_TYPE_CLIENT_GUIDE, RagDocument.title == GUIDE_TITLE
        )
    ).all()
    version_ids = session.scalars(
        select(RagDocumentVersion.version_id).where(RagDocumentVersion.doc_id.in_(doc_ids or [0]))
    ).all()
    if version_ids:
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "rag_document_version",
                AuditLog.entity_id.in_([str(version_id) for version_id in version_ids]),
            )
        )
        session.execute(delete(RagChunk).where(RagChunk.version_id.in_(version_ids)))
        session.execute(
            delete(RagDocumentVersion).where(RagDocumentVersion.version_id.in_(version_ids))
        )
    if doc_ids:
        session.execute(delete(RagDocument).where(RagDocument.doc_id.in_(doc_ids)))
    session.commit()


@pytest.fixture(autouse=True)
def clean(db: None):
    with SessionLocal() as session:
        _purge(session)
    yield
    with SessionLocal() as session:
        _purge(session)


@pytest.fixture
def client_fund(clean: None) -> int:
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance=BALANCE,
                n_purchases_returned=2,
                n_sales_returned=0,
                total_purchase_amount=BALANCE,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance=BALANCE,
                n_deposits=2,
                n_withdrawals=0,
            )
        )
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                sig_heavy_withdrawal=False,
                sig_dormant=True,
                sig_broken_pattern=False,
                sig_shrinking=False,
                sig_going_dormant=False,
                sig_never_repeated=False,
                risk_score=20,
                risk_band="Low",
                risk_reasons="none",
                fund_at_risk=0.0,
                config_version=1,
            )
        )
        session.commit()
        session.add(
            PiiVault(
                client_id=CLIENT_ID, client_name="Asha Njeri", contact_email="asha@example.com"
            )
        )
        session.commit()
    return CLIENT_ID


@pytest.fixture
def guide(clean: None) -> None:
    with SessionLocal() as session:
        added = add_guide(
            session,
            title=GUIDE_TITLE,
            topic="Funds",
            text=GUIDE_TEXT,
            created_by="writer",
            action_codes=NEEDS_A_GUIDE,
            embedder=EMB,
        )
        approve_guide_version(session, added.version_id, approved_by="lead")
        session.commit()


def _accepted_insight() -> int:
    with SessionLocal() as session:
        insight = AgentInsight(
            kind="opportunity",
            title=INSIGHT_TITLE,
            group_name="remaining actions test group",
            group_definition=GROUP_FILTER,
            client_count=1,
            money_total_kes=BALANCE,
            confidence="high",
            confidence_reason="the same group was counted twice",
            suggestion="reach out to this group",
            avoid_saying="anything about their balance",
            why_now="the group has waited long enough",
            state="accepted",
        )
        session.add(insight)
        session.commit()
        return insight.insight_id


def _drafter(model: ScriptedModel):
    def draft(session, *, campaign_id: int, prohibitions) -> int:
        guide_brief = guide_brief_for_campaign(session, campaign_id, at=TODAY)
        agent = EmailAgent(
            context_loader=lambda client_id, product: load_client_context(
                session, client_id, product, at=TODAY, extra_chunks=guide_brief.guides
            ),
            llm_client=model,
            prompt_builder=functools.partial(
                build_system_prompt,
                extra_prohibitions=tuple(prohibitions),
                mix_instruction=guide_brief.mix_instruction,
            ),
            max_attempts=1,
        )
        orchestrator = Orchestrator()
        orchestrator.register(agent)
        outcomes = run_due_enrollments(
            session,
            campaign_id=campaign_id,
            generate=lambda inner, enrollment, step_no: generate_for_enrollment(
                inner,
                enrollment,
                step_no,
                orchestrator=orchestrator,
                channel=agent.channel,
                settings=SETTINGS,
            ),
        )
        return sum(1 for outcome in outcomes if outcome.generated)

    return draft


def _no_drafts(session, *, campaign_id: int, prohibitions) -> int:
    return 0


def _recording_sender(sent: list[str]):
    def send(message: OutreachMessage) -> SendResult:
        sent.append(message.message_id)
        return SendResult(delivery_status="sent", sent_at=datetime.now(UTC))

    return send


def _propose_and_run(action_code: str, model: ScriptedModel, *, draft=None) -> dict:
    insight_id = _accepted_insight()
    with SessionLocal() as session:
        tools = make_write_tools(as_of=TODAY, draft=draft or _drafter(model))
        proposed = tools[WRITE_PROPOSAL](
            session,
            insight_id=insight_id,
            action_code=action_code,
            angle=action_code,
            reason="this response fits the group",
        )
        session.commit()
        if proposed.get("status") != "proposed":
            return {"proposed": proposed}
        transition_proposal(
            session,
            session.get(AgentProposal, proposed["proposal_id"]),
            to_status="approved",
            reason="a person read it and said yes",
            decided_by="reviewer_1",
        )
        session.commit()
        started = tools[RUN_PROPOSAL](session, proposal_id=proposed["proposal_id"])
        session.commit()
    return {"proposed": proposed, "started": started}


def _sign_off() -> str:
    with SessionLocal() as session:
        contract = load_tier(session, TIER, TODAY)
    return "" if contract is None else contract.sign_off


def test_every_remaining_action_is_on_at_suggest_only_with_its_own_brief(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, TODAY)
        angles = load_active_angles(session, TODAY)
        mappings = {row.situation: row.angle for row in load_active_mappings(session, TODAY)}

    for action_code in ACTIONS:
        action = actions[action_code]
        assert not action.paused
        assert action.default_permission == "suggest_only"
        assert action.channel == "email"
        assert action.message_angle == action_code
        brief = angles[action_code]
        assert brief.headline and brief.claim and brief.ask
        assert "Never promise a return or a rate" in brief.never
        assert "Never ask the client to confirm contact details" in brief.never
        assert "Do not state any figure that was not supplied as a fact" in brief.use

    for situation, angle in SITUATION_ANGLES.items():
        assert mappings[situation] == angle


@pytest.mark.parametrize("action_code", ACTIONS)
def test_each_action_goes_from_a_finding_to_a_sent_email(
    action_code: str, client_fund: int, guide: None
) -> None:
    model = ScriptedModel(_sign_off())
    result = _propose_and_run(action_code, model)
    sent: list[str] = []

    assert result["proposed"].get("status") == "proposed", result["proposed"]
    assert result["proposed"]["permission_applied"] == "suggest_only"
    assert result["started"]["drafted_count"] == 1
    assert result["started"]["account_managers_told"] == (1 if action_code == FOLLOW_UP else 0)

    with SessionLocal() as session:
        actions = load_active_actions(session, TODAY)
        brief = load_active_angles(session, TODAY)[action_code]
        message = session.scalars(
            select(OutreachMessage).where(OutreachMessage.client_id == client_fund)
        ).one()
        flagged = session.scalars(
            select(ActiveClientInteraction).where(
                ActiveClientInteraction.client_id == client_fund,
                ActiveClientInteraction.type == FLAGGED_FOR_ACCOUNT_MANAGER,
            )
        ).all()
        nothing_yet = send_due_touches(
            session, campaign_id=result["started"]["campaign_id"], sender=_recording_sender(sent)
        )
        session.commit()

    system = model.systems[0]
    assert message.status == "pending_review"
    assert brief.headline in system
    assert brief.never in system
    assert MIX_INSTRUCTIONS[actions[action_code].content_mix] in system
    assert (GUIDE_TEXT in system) == (action_code in NEEDS_A_GUIDE)
    assert len(flagged) == (1 if action_code == FOLLOW_UP else 0)
    assert sent == [] and not nothing_yet

    with SessionLocal() as session:
        decide(session, message.message_id, outcome="approve", reviewer_id="reviewer_1")
        session.commit()
        went = send_due_touches(
            session, campaign_id=result["started"]["campaign_id"], sender=_recording_sender(sent)
        )
        session.commit()

    assert [outcome.sent for outcome in went] == [True]
    assert sent == [message.message_id]


def test_the_account_manager_is_told_only_when_a_draft_was_made(
    client_fund: int, guide: None
) -> None:
    result = _propose_and_run(FOLLOW_UP, ScriptedModel(_sign_off()), draft=_no_drafts)

    with SessionLocal() as session:
        flagged = session.scalars(
            select(ActiveClientInteraction).where(
                ActiveClientInteraction.client_id == client_fund,
                ActiveClientInteraction.type == FLAGGED_FOR_ACCOUNT_MANAGER,
            )
        ).all()

    assert result["started"]["drafted_count"] == 0
    assert result["started"]["account_managers_told"] == 0
    assert flagged == []


@pytest.mark.parametrize("action_code", NEEDS_A_GUIDE)
def test_nothing_is_proposed_without_an_approved_guide(action_code: str, client_fund: int) -> None:
    result = _propose_and_run(action_code, ScriptedModel(_sign_off()))

    assert result["proposed"]["error"] == "every_client_was_left_out"

    with SessionLocal() as session:
        action = load_active_actions(session, TODAY)[action_code]
        reasons = group_skip_reasons(
            session,
            [GroupMember(client_id=client_fund, unit_fund_id=FUND_ID, balance=BALANCE)],
            action,
            TODAY,
            None,
        )
    assert set(reasons.values()) == {NO_APPROVED_GUIDE}


def test_a_second_fund_never_runs_above_approve_each_and_is_offered_once(
    client_fund: int, guide: None
) -> None:
    with SessionLocal() as session:
        set_permission(
            session,
            SECOND_FUND,
            "act_alone",
            changed_by="test",
            changed_reason="a test of the cap on a second fund",
        )
        level = effective_permission(session, SECOND_FUND)
        session.rollback()
    assert level == "approve_each"

    member = GroupMember(client_id=client_fund, unit_fund_id=FUND_ID, balance=BALANCE)
    with SessionLocal() as session:
        action = load_active_actions(session, TODAY)[SECOND_FUND]
        before = member_skip_reason(session, member, action, TODAY, 7)

    model = ScriptedModel(_sign_off())
    result = _propose_and_run(SECOND_FUND, model)
    with SessionLocal() as session:
        message = session.scalars(
            select(OutreachMessage).where(OutreachMessage.client_id == client_fund)
        ).one()
        decide(session, message.message_id, outcome="approve", reviewer_id="reviewer_1")
        session.commit()
        send_due_touches(
            session, campaign_id=result["started"]["campaign_id"], sender=_recording_sender([])
        )
        session.commit()
        after = member_skip_reason(session, member, action, TODAY, 7)

    assert before is None
    assert after == ALREADY_OFFERED
