"""The fee warning, all the way from a finding to an email that went.

One test walks the whole path: the agent writes a finding, a person accepts
it, the response is proposed, a person approves the proposal, the group is
enrolled and drafted, the draft waits in the review queue, a person approves
the message, and only then does it send. The rest of the file holds the two
places a person must stand in the way, and the briefs the two live actions
were written against.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from app.agents.action_catalog import load_active_actions
from app.agents.email_channel import EmailAgent
from app.agents.graph import load_client_context, month_the_account_empties
from app.agents.guardrails import (
    default_numeric_traceability_check,
    default_sign_off_check,
    traceable_numbers,
)
from app.agents.permissions import effective_permission
from app.agents.proposal_state import transition_proposal
from app.agents.watchlist import FEES_WILL_EMPTY
from app.agents.write_tools import RUN_PROPOSAL, WRITE_PROPOSAL, make_write_tools
from app.campaigns.generation import generate_for_enrollment
from app.campaigns.touch import SendResult, run_due_enrollments, send_due_touches
from app.config import Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
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
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.rules.catalog import load_active_angles
from app.rules.tier_contract import load_tier
from app.services.review import decide

FUND_ID = 9746
CLIENT_ID = 974601

INSIGHT_TITLE = "A group the first two actions were run against"

LIVE_ACTIONS = ("welcome_and_top_up", "fee_warning")

# The tier a client with no risk history behind them falls to.
TIER = "T3"

TODAY = date.today()


class ScriptedModel:
    """Stands in for the drafting model, writing back the fact it was given.

    A real model is asked to reproduce the month exactly as supplied. This
    one does exactly that, so the draft that comes out is one a real model
    could have written and every guardrail runs on it for real.
    """

    model = "scripted"

    def __init__(self, sign_off: str) -> None:
        self.sign_off = sign_off
        self.systems: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.systems.append(system)
        facts = dict(line.split(": ", 1) for line in user.splitlines() if ": " in line)
        month = facts.get("month_the_account_empties", "")
        body = (
            "Hi {{first_name}}, on the way things are going your {{fund_name}} "
            f"account reaches zero in {month}. Adding a little to it before then "
            "keeps it working for you, and we are here if you would like to "
            f"talk it through.\n{self.sign_off}"
        )
        return json.dumps({"subject": "About your account", "body": body})


def _purge(session) -> None:
    message_ids = session.scalars(
        select(OutreachMessage.message_id).where(OutreachMessage.client_id == CLIENT_ID)
    ).all()
    if message_ids:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(
            delete(TouchLog).where(TouchLog.message_id.in_(message_ids)),
        )
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

    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT_ID))
    session.execute(
        delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == CLIENT_ID)
    )
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == CLIENT_ID))
    session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
    session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
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
    """One client of the active book whose balance runs out in a few months."""
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance=8_000.0,
                n_purchases_returned=2,
                n_sales_returned=0,
                total_purchase_amount=8_000.0,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                balance=8_000.0,
                n_deposits=2,
                n_withdrawals=0,
                months_until_empty=3.0,
            )
        )
        session.commit()
        session.add(
            PiiVault(
                client_id=CLIENT_ID,
                client_name="Asha Njeri",
                contact_email="asha@example.com",
            )
        )
        session.commit()
    return CLIENT_ID


def _accepted_insight() -> int:
    with SessionLocal() as session:
        insight = AgentInsight(
            kind="risk",
            title=INSIGHT_TITLE,
            group_name=FEES_WILL_EMPTY,
            client_count=1,
            money_total_kes=8_000.0,
            confidence="high",
            confidence_reason="the same balance and the same fee were counted twice",
            suggestion="tell them before the balance runs out",
            avoid_saying="that their account is nearly empty",
            why_now="the fee takes another month off it every month",
            state="accepted",
        )
        session.add(insight)
        session.commit()
        return insight.insight_id


def _sign_off(session) -> str:
    """The sign off this client's tier asks for, which the draft must carry."""
    contract = load_tier(session, TIER, TODAY)
    return "" if contract is None else contract.sign_off


def _drafter(model: ScriptedModel):
    """The real drafting run, with the model scripted and nothing else faked."""

    def draft(session, *, campaign_id: int, prohibitions) -> int:
        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="test-key",
            llm_model="claude-opus-5",
            llm_temperature=None,
            llm_max_tokens=1024,
        )
        agent = EmailAgent(
            context_loader=lambda client_id, product: load_client_context(
                session, client_id, product, at=TODAY
            ),
            llm_client=model,
            max_attempts=1,
        )
        outcomes = run_due_enrollments(
            session,
            campaign_id=campaign_id,
            generate=lambda inner, enrollment, step_no: generate_for_enrollment(
                inner, enrollment, step_no, agent=agent, settings=settings
            ),
        )
        return sum(1 for outcome in outcomes if outcome.generated)

    return draft


def test_a_finding_becomes_an_approved_email_with_a_person_at_both_gates(
    client_fund: int,
) -> None:
    insight_id = _accepted_insight()
    sent: list[str] = []

    with SessionLocal() as session:
        model = ScriptedModel(_sign_off(session))
        tools = make_write_tools(as_of=TODAY, draft=_drafter(model))

        proposed = tools[WRITE_PROPOSAL](
            session,
            insight_id=insight_id,
            action_code="fee_warning",
            angle="fee_warning",
            reason="their balance runs out within a few months at the current fee",
        )
        session.commit()
        proposal_id = proposed["proposal_id"]

        transition_proposal(
            session,
            session.get(AgentProposal, proposal_id),
            to_status="approved",
            reason="a person read it and said yes",
            decided_by="reviewer_1",
        )
        session.commit()

        started = tools[RUN_PROPOSAL](session, proposal_id=proposal_id)
        session.commit()

        month = month_the_account_empties(session, client_fund)
        waiting = session.scalars(
            select(OutreachMessage).where(OutreachMessage.client_id == client_fund)
        ).all()

    assert proposed["status"] == "proposed"
    assert started["drafted_count"] == 1
    assert len(waiting) == 1
    message = waiting[0]
    assert message.status == "pending_review"
    assert month is not None
    assert month in message.ai_draft_content["body"]
    assert message.personalized_content["body"].startswith("Hi Asha")

    with SessionLocal() as session:
        nothing_yet = send_due_touches(
            session, campaign_id=started["campaign_id"], sender=_recording_sender(sent)
        )
        session.commit()

    assert sent == []
    assert [outcome.reason for outcome in nothing_yet] == []

    with SessionLocal() as session:
        decide(session, message.message_id, outcome="approve", reviewer_id="reviewer_1")
        session.commit()
        went = send_due_touches(
            session, campaign_id=started["campaign_id"], sender=_recording_sender(sent)
        )
        session.commit()

    assert [outcome.sent for outcome in went] == [True]
    assert sent == [message.message_id]

    with SessionLocal() as session:
        after = session.get(OutreachMessage, message.message_id)
        touch = session.scalar(select(TouchLog).where(TouchLog.message_id == message.message_id))

    assert after.status == "approved"
    assert touch.sent_at is not None


def _recording_sender(sent: list[str]):
    """Stands where the mailer would, so the send is real up to the last step."""

    def send(message: OutreachMessage) -> SendResult:
        sent.append(message.message_id)
        return SendResult(delivery_status="recorded", sent_at=datetime.now(UTC))

    return send


def test_only_the_two_first_actions_are_switched_on(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, TODAY)

    running = {code for code, row in actions.items() if not row.paused}
    assert set(LIVE_ACTIONS) <= running
    assert {code for code in running if actions[code].channel is not None} == set(LIVE_ACTIONS)


def test_both_live_actions_need_every_message_reviewed(db: None) -> None:
    with SessionLocal() as session:
        for action_code in LIVE_ACTIONS:
            assert effective_permission(session, action_code) == "approve_each"


def test_each_live_action_has_a_brief_written_for_it(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, TODAY)
        angles = load_active_angles(session, TODAY)

    for action_code in LIVE_ACTIONS:
        angle = actions[action_code].message_angle
        assert angle == action_code
        brief = angles[angle]
        assert brief.headline and brief.claim and brief.ask
        assert "Never promise a return or a rate" in brief.never


def test_a_brief_lets_no_number_through_that_traces_to_nothing(db: None) -> None:
    """The month is a supplied fact, so naming it is traceable and inventing is not."""
    facts = {"month_the_account_empties": "December 2026"}
    allowed = traceable_numbers(facts)

    default_numeric_traceability_check(
        {"body": "your account reaches zero in December 2026", "facts": facts}
    )
    assert "2026" in allowed

    with pytest.raises(Exception, match="numbers that trace to no fact"):
        default_numeric_traceability_check(
            {"body": "your balance of 8,000 runs out in December 2026", "facts": facts}
        )


def test_the_fee_warning_brief_only_allows_the_month_it_was_given(db: None) -> None:
    with SessionLocal() as session:
        brief = load_active_angles(session, TODAY)["fee_warning"]

    assert "name it" in brief.use
    assert "how many months are left as a number" in brief.never
    assert "nearly empty" in brief.never


def test_a_draft_without_the_tiers_sign_off_is_refused(db: None) -> None:
    with SessionLocal() as session:
        contract = load_tier(session, TIER, TODAY)

    with pytest.raises(Exception, match="missing the required sign off"):
        default_sign_off_check({"body": "no sign off here", "contract": contract})
