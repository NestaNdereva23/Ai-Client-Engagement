"""Template drafting: one draft per bucket, persisted as a message_template."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select

from app.agents.email_agent import conditional_prohibitions
from app.agents.graph import ClientContext
from app.campaigns.bucketing import Bucket, BucketMember, ProfileKey
from app.campaigns.template_generation import (
    _bucket_prompt_builder,
    bucket_context,
    bucket_facts,
    bucket_placeholder_chunks,
    draft_template,
)
from app.config import Settings
from app.db.models.campaigns import Enrollment
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.message_template import MessageTemplate
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal

FUND_ID = 971
CLIENT_ID = 97101


def make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


class ScriptedLLMClient:
    """Returns each draft in order, one per generate() call."""

    model = "stub"

    def __init__(self, drafts: list[str]) -> None:
        self._drafts = list(drafts)
        self.calls: list[dict] = []

    def generate(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self._drafts.pop(0)


def draft_json(subject: str = "Come back to {{fund_name}}", body: str = "") -> str:
    return json.dumps({"subject": subject, "body": body})


def make_bucket(profile_key: ProfileKey, *, client_id: int = CLIENT_ID) -> Bucket:
    """One bucket with one member, enough for draft_template to run against."""
    context = ClientContext(
        raw_context={},
        angle=profile_key.message_angle,
        prompt_variant=profile_key.message_angle,
        priority_tier=profile_key.priority_tier,
        chunks=(),
        facts={},
    )
    enrollment = Enrollment(enrollment_id=1, campaign_id=1, client_id=client_id, current_step=0)
    return Bucket(
        profile_key=profile_key,
        members=[BucketMember(enrollment=enrollment, context=context)],
    )


def test_bucket_facts_carries_only_the_narrow_scan_safe_fields() -> None:
    key = ProfileKey(
        message_angle="back_on_schedule",
        priority_tier="T3",
        product="money market",
        has_cadence=True,
        stale_contact=True,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    assert bucket_facts(key) == {"stale_contact": True}


def test_bucket_facts_includes_exit_reason_only_when_charge_settled() -> None:
    settled = ProfileKey(
        message_angle="not_a_goodbye",
        priority_tier="T4",
        product="money market",
        has_cadence=False,
        stale_contact=False,
        exit_reason_charge_settled=True,
        fund_name_known=False,
    )
    not_settled = ProfileKey(
        message_angle="pick_up_again",
        priority_tier="T3",
        product="money market",
        has_cadence=False,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    assert bucket_facts(settled)["exit_reason"] == "charge_settled"
    assert "exit_reason" not in bucket_facts(not_settled)


def test_bucket_facts_resolves_a_real_fund_name_when_known() -> None:
    key = ProfileKey(
        message_angle="not_a_goodbye",
        priority_tier="T4",
        product="money market",
        has_cadence=False,
        stale_contact=False,
        exit_reason_charge_settled=True,
        fund_name_known=True,
    )
    assert bucket_facts(key)["fund_name"] == "Cytonn Money Market Fund"


def test_bucket_placeholder_chunks_carries_a_token_for_every_placeholder_filled_fact() -> None:
    key = ProfileKey(
        message_angle="back_on_schedule",
        priority_tier="T3",
        product="money market",
        has_cadence=True,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    text = " ".join(chunk.text for chunk in bucket_placeholder_chunks(key))
    for token in (
        "{{typical_contribution}}",
        "{{largest_contribution}}",
        "{{years_since_exit}}",
        "{{days_held_after_last_topup}}",
        "{{month_they_left}}",
        "{{cadence_interval_days}}",
    ):
        assert token in text


def test_bucket_placeholder_chunks_omits_cadence_when_the_bucket_has_none() -> None:
    key = ProfileKey(
        message_angle="pick_up_again",
        priority_tier="T3",
        product="money market",
        has_cadence=False,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    text = " ".join(chunk.text for chunk in bucket_placeholder_chunks(key))
    assert "{{cadence_interval_days}}" not in text
    assert "{{typical_contribution}}" in text


def test_bucket_facts_drives_the_same_conditional_prohibitions_as_the_profile() -> None:
    key = ProfileKey(
        message_angle="not_a_goodbye",
        priority_tier="T4",
        product="money market",
        has_cadence=False,
        stale_contact=True,
        exit_reason_charge_settled=True,
        fund_name_known=False,
    )
    prohibitions = conditional_prohibitions(bucket_facts(key))
    assert any("over three years old" in line for line in prohibitions)
    assert any("charge, not a" in line for line in prohibitions)


def test_bucket_prompt_builder_reflects_has_cadence_regardless_of_the_facts_it_is_given() -> None:
    with_cadence = _bucket_prompt_builder(
        ProfileKey(
            message_angle="back_on_schedule",
            priority_tier="T3",
            product="money market",
            has_cadence=True,
            stale_contact=False,
            exit_reason_charge_settled=False,
            fund_name_known=False,
        )
    )
    without_cadence = _bucket_prompt_builder(
        ProfileKey(
            message_angle="pick_up_again",
            priority_tier="T3",
            product="money market",
            has_cadence=False,
            stale_contact=False,
            exit_reason_charge_settled=False,
            fund_name_known=False,
        )
    )
    # Both called with the same (empty) facts, the shape the graph actually
    # passes -- the difference in output must come from the closure alone.
    prompt_with = with_cadence(
        angle="back_on_schedule", prompt_variant="back_on_schedule", facts={}
    )
    prompt_without = without_cadence(
        angle="pick_up_again", prompt_variant="pick_up_again", facts={}
    )
    assert "no measurable cadence" not in prompt_with
    assert "no measurable cadence" in prompt_without


def test_bucket_context_replaces_facts_and_adds_placeholder_chunks() -> None:
    key = ProfileKey(
        message_angle="pick_up_again",
        priority_tier="T3",
        product="money market",
        has_cadence=True,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    bucket = make_bucket(key)
    context = bucket_context(bucket)
    assert context.angle == "pick_up_again"
    assert context.priority_tier == "T3"
    assert context.raw_context == {}
    assert context.facts == bucket_facts(key)
    chunk_text = " ".join(chunk.text for chunk in context.chunks)
    assert "{{typical_contribution}}" in chunk_text


@pytest.fixture
def client(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
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

    yield CLIENT_ID

    with SessionLocal() as session:
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id == CLIENT_ID)
        ).all()
        if run_ids:
            template_ids = session.scalars(
                select(MessageTemplate.template_id).where(
                    MessageTemplate.generation_run_id.in_(run_ids)
                )
            ).all()
            if template_ids:
                session.execute(
                    delete(MessageTemplate).where(MessageTemplate.template_id.in_(template_ids))
                )
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
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


@pytest.fixture
def campaign(client: int):
    with SessionLocal() as session:
        row = Campaign(name="template generation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(MessageTemplate).where(MessageTemplate.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_draft_template_persists_a_run_and_a_message_template_on_accept(
    campaign: int, client: int
) -> None:
    key = ProfileKey(
        message_angle="pick_up_again",
        priority_tier="T3",
        product="money market",
        has_cadence=False,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    bucket = make_bucket(key, client_id=client)
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, come back to us.")])
    settings = make_settings()

    with SessionLocal() as session:
        template = draft_template(
            session, bucket, campaign_id=campaign, settings=settings, llm_client=llm
        )
        session.commit()

    assert template is not None
    assert template.profile_key == key.as_dict()
    assert template.ai_draft_content == {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, come back to us.",
    }
    assert template.status == "pending_review"

    with SessionLocal() as session:
        run = session.get(GenerationRun, template.generation_run_id)
        assert run is not None
        assert run.client_id == client
        assert run.priority_tier == "T3"
        stored = session.get(MessageTemplate, template.template_id)
        assert stored is not None


def test_draft_template_clears_the_privacy_boundary_with_every_placeholder_fact_in_play(
    campaign: int, client: int
) -> None:
    """Every placeholder-filled fact and the cadence token all in play at
    once must still clear scan_inbound's ModelFactBlock check."""
    key = ProfileKey(
        message_angle="back_on_schedule",
        priority_tier="T3",
        product="money market",
        has_cadence=True,
        stale_contact=True,
        exit_reason_charge_settled=False,
        fund_name_known=True,
    )
    bucket = make_bucket(key, client_id=client)
    llm = ScriptedLLMClient(
        [draft_json(body="Dear {{first_name}}, resume your {{cadence_interval_days}}-day habit.")]
    )
    settings = make_settings()

    with SessionLocal() as session:
        template = draft_template(
            session, bucket, campaign_id=campaign, settings=settings, llm_client=llm
        )
        session.commit()

    assert template is not None
    assert "{{cadence_interval_days}}" in template.ai_draft_content["body"]
    assert "{{cadence_interval_days}}" in llm.calls[0]["system"]
    assert "cadence_interval_days" not in llm.calls[0]["user"]


def test_draft_template_returns_none_and_still_persists_a_rejected_run(
    campaign: int, client: int
) -> None:
    key = ProfileKey(
        message_angle="pick_up_again",
        priority_tier="T3",
        product="money market",
        has_cadence=False,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    bucket = make_bucket(key, client_id=client)
    llm = ScriptedLLMClient(["not json at all", "still not json"])
    settings = make_settings()

    with SessionLocal() as session:
        template = draft_template(
            session, bucket, campaign_id=campaign, settings=settings, llm_client=llm
        )
        session.commit()

    assert template is None

    with SessionLocal() as session:
        runs = session.scalars(select(GenerationRun).where(GenerationRun.client_id == client)).all()
        assert len(runs) == 1
        assert runs[0].status == "rejected"
        templates = session.scalars(
            select(MessageTemplate).where(MessageTemplate.generation_run_id == runs[0].run_id)
        ).all()
        assert templates == []
