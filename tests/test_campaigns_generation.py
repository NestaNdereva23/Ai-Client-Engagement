"""Wiring the real generation pipeline into a campaign touch, and into a
reviewer's request to regenerate a still-pending draft.

resolve_product covers deriving the RAG-facing product string from a
client's own fund_type band, defaulting gracefully with no facts row.
generate_for_enrollment (the real GenerateFn) covers an accepted draft
becoming a pending-review message, and a rejected draft returning None
with the run still persisted for guardrail metrics. regenerate_message
covers replacing a still-pending message's draft, refusing one already
decided, and leaving the original untouched when the fresh attempt is
itself rejected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select

from app.agents.email_channel import EmailAgent
from app.agents.graph import ClientContext
from app.campaigns.enrollment import enroll_cohort
from app.campaigns.generation import (
    MessageNotRegenerable,
    RegenerationRejected,
    generate_for_enrollment,
    regenerate_message,
    resolve_product,
)
from app.config import Settings
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
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
from app.db.session import SessionLocal
from app.services.campaigns import add_campaign_step, run_campaign_generation
from app.services.review import MessageNotFound, decide, get_message

FUND_ID = 985
CLIENT_ID = 98501


def make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def make_context_loader():
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={"client_id": client_id},
            angle="winback_habit",
            prompt_variant="winback_habit",
            chunks=(),
        )

    return load


def draft_json(subject: str = "Come back to {{fund_name}}", body: str = "") -> str:
    return json.dumps({"subject": subject, "body": body})


class ScriptedLLMClient:
    model = "stub"

    def __init__(self, drafts: list[str]) -> None:
        self._drafts = list(drafts)

    def generate(self, *, system: str, user: str) -> str:
        return self._drafts.pop(0)


def make_agent(drafts: list[str]) -> EmailAgent:
    kwargs = {"max_attempts": 1} if len(drafts) == 1 else {}
    return EmailAgent(
        context_loader=make_context_loader(),
        llm_client=ScriptedLLMClient(drafts),
        **kwargs,
    )


@pytest.fixture
def client(db: None):
    """One fund (a money market fund, so resolve_product has a real band to
    read), one named client, ready to grow campaigns/messages on demand.
    """
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        session.add(
            PiiVault(client_id=CLIENT_ID, client_name="Jane Doe", contact_email="jane@example.com")
        )
        session.add(
            ClientFeatures(client_id=CLIENT_ID, fund_type="money_market", purchase_depth="single")
        )
        session.commit()

    yield CLIENT_ID

    with SessionLocal() as session:
        session.execute(delete(OutreachMessage).where(OutreachMessage.client_id == CLIENT_ID))
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
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == CLIENT_ID))
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


@pytest.fixture
def campaign(client: int):
    with SessionLocal() as session:
        campaign_row = Campaign(name="generation wiring test campaign")
        session.add(campaign_row)
        session.commit()
        campaign_id = campaign_row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(
                Enrollment.campaign_id == campaign_id, Enrollment.client_id == client
            )
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(
            delete(Enrollment).where(
                Enrollment.campaign_id == campaign_id, Enrollment.client_id == client
            )
        )
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        # Runs before client's own teardown (fixtures unwind in reverse setup
        # order, and campaign is requested after client), so any message a
        # test created against this campaign must be cleared here first, or
        # the FK on outreach_message.campaign_id blocks deleting the campaign.
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.campaign_id == campaign_id)
        ).all()
        if message_ids:
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_resolve_product_reads_the_clients_fund_type_band(client: int) -> None:
    with SessionLocal() as session:
        assert resolve_product(session, client) == "money market"


def test_resolve_product_defaults_to_other_with_no_features_row(db: None) -> None:
    fund_id, client_id = 986, 98601
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        assert resolve_product(session, client_id) == "other"

    with SessionLocal() as session:
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_generate_for_enrollment_creates_a_pending_review_message_on_acceptance(
    campaign: int, client: int
) -> None:
    enrollment = Enrollment(campaign_id=campaign, client_id=client)
    agent = make_agent(
        [draft_json(body="Dear {{first_name}}, {{fund_name}} misses you back on schedule.")]
    )
    settings = make_settings()

    with SessionLocal() as session:
        message = generate_for_enrollment(session, enrollment, 1, agent=agent, settings=settings)
        session.commit()

    assert message is not None
    assert message.status == "pending_review"
    assert message.campaign_id == campaign
    assert message.client_id == client


def test_generate_for_enrollment_returns_none_and_still_persists_a_rejected_run(
    campaign: int, client: int
) -> None:
    enrollment = Enrollment(campaign_id=campaign, client_id=client)
    # Body is far below the guardrail's minimum length, at max_attempts=1.
    agent = make_agent([draft_json(subject="Hi {{first_name}}", body="{{fund_name}}")])
    settings = make_settings()

    with SessionLocal() as session:
        message = generate_for_enrollment(session, enrollment, 1, agent=agent, settings=settings)
        session.commit()

    assert message is None

    with SessionLocal() as session:
        runs = session.scalars(select(GenerationRun).where(GenerationRun.client_id == client)).all()
    assert len(runs) == 1
    assert runs[0].status == "rejected"


def test_regenerate_message_replaces_the_draft_and_keeps_the_campaign(
    campaign: int, client: int
) -> None:
    agent = make_agent([draft_json(body="Dear {{first_name}}, the original draft.")])
    settings = make_settings()
    with SessionLocal() as session:
        enrollment = Enrollment(campaign_id=campaign, client_id=client)
        original = generate_for_enrollment(session, enrollment, 1, agent=agent, settings=settings)
        session.commit()
        original_id = original.message_id

    fresh_agent = make_agent(
        [draft_json(body="Dear {{first_name}}, a completely different draft.")]
    )
    with SessionLocal() as session:
        fresh = regenerate_message(session, original_id, agent=fresh_agent, settings=settings)
        session.commit()
        fresh_id = fresh.message_id

    assert fresh_id != original_id
    assert fresh.campaign_id == campaign
    assert fresh.client_id == client
    assert "completely different" in fresh.ai_draft_content["body"]

    with SessionLocal() as session:
        with pytest.raises(MessageNotFound):
            get_message(session, original_id)
        assert get_message(session, fresh_id) is not None


def test_regenerate_message_refuses_an_already_decided_message(campaign: int, client: int) -> None:
    agent = make_agent([draft_json(body="Dear {{first_name}}, the original draft.")])
    settings = make_settings()
    with SessionLocal() as session:
        enrollment = Enrollment(campaign_id=campaign, client_id=client)
        message = generate_for_enrollment(session, enrollment, 1, agent=agent, settings=settings)
        session.commit()
        message_id = message.message_id
        decide(session, message_id, outcome="approve", reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session, pytest.raises(MessageNotRegenerable) as excinfo:
        regenerate_message(session, message_id, agent=agent, settings=settings)
    assert excinfo.value.status == "approved"


def test_regenerate_message_leaves_the_original_untouched_when_the_fresh_attempt_is_rejected(
    campaign: int, client: int
) -> None:
    agent = make_agent([draft_json(body="Dear {{first_name}}, the original draft that survives.")])
    settings = make_settings()
    with SessionLocal() as session:
        enrollment = Enrollment(campaign_id=campaign, client_id=client)
        message = generate_for_enrollment(session, enrollment, 1, agent=agent, settings=settings)
        session.commit()
        message_id = message.message_id

    failing_agent = make_agent([draft_json(subject="Hi {{first_name}}", body="{{fund_name}}")])
    with SessionLocal() as session:
        with pytest.raises(RegenerationRejected):
            regenerate_message(session, message_id, agent=failing_agent, settings=settings)
        session.rollback()

    with SessionLocal() as session:
        still_there = get_message(session, message_id)
        assert "survives" in still_there.ai_draft_content["body"]
        assert still_there.status == "pending_review"


def test_regenerate_message_repoints_the_touch_that_produced_it(campaign: int, client: int) -> None:
    """A message generated through a campaign has a touch_log row on it.

    Regenerating replaces the message, so the touch has to follow it to the
    replacement rather than be orphaned on a deleted one.
    """
    agent = make_agent([draft_json(body="Dear {{first_name}}, the original campaign draft.")])
    settings = make_settings()
    with SessionLocal() as session:
        add_campaign_step(session, campaign, offset_days=0, message_angle="winback_habit")
        session.commit()
        enroll_cohort(session, campaign_id=campaign, client_ids=[client])
        session.commit()
        run_campaign_generation(session, campaign, agent=agent, settings=settings)
        session.commit()
        # Scoped to this test's own enrollment: "the one touch_log row in
        # the whole database" is not a safe assumption in a shared,
        # uncleaned test database running alongside every other test.
        enrollment_id = session.scalar(
            select(Enrollment.enrollment_id).where(
                Enrollment.campaign_id == campaign, Enrollment.client_id == client
            )
        )
        touch = session.scalars(
            select(TouchLog).where(TouchLog.enrollment_id == enrollment_id)
        ).one()
        original_id = touch.message_id
        touch_id = touch.touch_id

    assert original_id is not None

    fresh_agent = make_agent(
        [draft_json(body="Dear {{first_name}}, a regenerated campaign draft.")]
    )
    with SessionLocal() as session:
        fresh = regenerate_message(session, original_id, agent=fresh_agent, settings=settings)
        session.commit()
        fresh_id = fresh.message_id

    with SessionLocal() as session:
        assert session.get(TouchLog, touch_id).message_id == fresh_id
        with pytest.raises(MessageNotFound):
            get_message(session, original_id)


def test_run_campaign_generation_generates_the_due_enrollment(campaign: int, client: int) -> None:
    agent = make_agent([draft_json(body="Dear {{first_name}}, back on schedule as promised.")])
    settings = make_settings()

    with SessionLocal() as session:
        add_campaign_step(session, campaign, offset_days=0, message_angle="winback_habit")
        session.commit()
        enroll_cohort(session, campaign_id=campaign, client_ids=[client])
        session.commit()
        outcomes = run_campaign_generation(session, campaign, agent=agent, settings=settings)
        session.commit()

    assert len(outcomes) == 1
    assert outcomes[0].generated is True

    with SessionLocal() as session:
        messages = session.scalars(
            select(OutreachMessage).where(OutreachMessage.campaign_id == campaign)
        ).all()
    assert len(messages) == 1
    assert messages[0].status == "pending_review"
