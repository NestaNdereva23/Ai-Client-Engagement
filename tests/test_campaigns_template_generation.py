"""Template drafting: one draft per bucket, persisted as a message_template."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select, text

from app.agents.email_agent import conditional_prohibitions
from app.agents.graph import ClientContext
from app.campaigns.bucketing import Bucket, BucketMember, ProfileKey
from app.campaigns.template_generation import (
    _bucket_prompt_builder,
    bucket_context,
    bucket_facts,
    bucket_placeholder_chunks,
    draft_template,
    draft_templates_for_campaign,
)
from app.campaigns.template_policy import set_campaign_policy
from app.config import Settings
from app.db.models.campaigns import CampaignStep, Enrollment
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
from app.db.models.models import ClientFeatures, ClientFund, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.models.template_generation_plan import TemplateGenerationPlan
from app.db.models.template_policy import CampaignTemplatePolicy
from app.db.session import SessionLocal
from app.services.campaigns import add_campaign_step

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


# ---------------------------------------------------------------------------
# draft_templates_for_campaign: limit-aware, top-up-safe, deterministic order.
# ---------------------------------------------------------------------------

MULTI_FUND_ID = 9711
BIG = (971101, 971102, 971103)  # money market, 3 clients -- wins on size alone
TIE_HIGH = (971104, 971105)  # high yield, 2 clients, large observed_volume
TIE_LOW = (971106, 971107)  # fixed income, 2 clients, small observed_volume
ALL_MULTI_CLIENT_IDS = BIG + TIE_HIGH + TIE_LOW
# The order draft_templates_for_campaign must produce every time: bucket
# size descending (3 beats 2 and 2), then total observed volume descending
# breaks the tie between the two size-2 buckets.
EXPECTED_PRODUCT_ORDER = ["money market", "high yield", "fixed income"]


def _fixed_context_loader(client_id: int, product: str) -> ClientContext:
    """Every client resolves to the same angle and tier; only product (read
    straight off ClientFeatures.fund_type by resolve_product, not from this
    loader) tells one bucket apart from another.
    """
    return ClientContext(
        raw_context={},
        angle="pick_up_again",
        prompt_variant="pick_up_again",
        priority_tier="T3",
        chunks=(),
        facts={},
    )


def _seed_multi_client(session, client_id: int, *, fund_type: str, observed_volume: float) -> None:
    session.add(
        Clients(
            client_id=client_id,
            unit_fund_id=MULTI_FUND_ID,
            n_purchases_returned=1,
            n_sales_returned=1,
        )
    )
    session.flush()
    session.add(ClientFeatures(client_id=client_id, fund_type=fund_type, purchase_depth="single"))
    session.add(
        PiiVault(
            client_id=client_id,
            client_name=f"Multi Bucket Test {client_id}",
            contact_email="present@example.com",
            opt_out_flag=False,
        )
    )
    session.add(
        ClientFund(
            client_id=client_id,
            unit_fund_id=MULTI_FUND_ID,
            is_primary_contact_row=True,
            n_purchases=1,
            n_sales=0,
            observed_volume=observed_volume,
        )
    )


def _purge_multi_rows(session) -> None:
    session.execute(delete(ClientFund).where(ClientFund.client_id.in_(ALL_MULTI_CLIENT_IDS)))
    session.execute(delete(PiiVault).where(PiiVault.client_id.in_(ALL_MULTI_CLIENT_IDS)))
    session.execute(
        delete(ClientFeatures).where(ClientFeatures.client_id.in_(ALL_MULTI_CLIENT_IDS))
    )
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_MULTI_CLIENT_IDS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == MULTI_FUND_ID))
    session.commit()


@pytest.fixture
def multi_bucket_cohort(db: None):
    """Seven clients across three buckets sized 3, 2, 2, so ordering has both
    a size winner and a tie the observed_volume tie-break has to settle.
    """
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")

    with SessionLocal() as session:
        _purge_multi_rows(session)
        session.add(Funds(unit_fund_id=MULTI_FUND_ID, unit_fund_name="Multi Bucket Test Fund"))
        session.commit()

        for client_id in BIG:
            _seed_multi_client(session, client_id, fund_type="money_market", observed_volume=10)
        for client_id in TIE_HIGH:
            _seed_multi_client(session, client_id, fund_type="high_yield", observed_volume=100_000)
        for client_id in TIE_LOW:
            _seed_multi_client(session, client_id, fund_type="fixed_income", observed_volume=500)
        session.commit()

    with SessionLocal() as session:
        row = Campaign(name="multi bucket template generation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id
        add_campaign_step(session, campaign_id, offset_days=0, message_angle="pick_up_again")
        session.commit()
        session.add_all(
            [
                Enrollment(
                    campaign_id=campaign_id, client_id=client_id, is_primary_contact_row=True
                )
                for client_id in ALL_MULTI_CLIENT_IDS
            ]
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id.in_(ALL_MULTI_CLIENT_IDS))
        ).all()
        if run_ids:
            session.execute(
                delete(MessageTemplate).where(MessageTemplate.generation_run_id.in_(run_ids))
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
        session.execute(
            delete(TemplateGenerationPlan).where(TemplateGenerationPlan.campaign_id == campaign_id)
        )
        session.execute(
            delete(CampaignTemplatePolicy).where(CampaignTemplatePolicy.campaign_id == campaign_id)
        )
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()
        _purge_multi_rows(session)


def test_deterministic_order_with_no_limit_drafts_every_bucket(multi_bucket_cohort: int) -> None:
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, come back to us.")] * 3)

    with SessionLocal() as session:
        outcome = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()

    assert outcome.estimated_templates == 3
    assert outcome.effective_limit is None
    assert outcome.drafted_count == 3
    assert outcome.skipped_existing == 0
    assert outcome.failed_guardrails == 0
    assert [t.profile_key["product"] for t in outcome.templates] == EXPECTED_PRODUCT_ORDER

    with SessionLocal() as session:
        plan = session.scalar(
            select(TemplateGenerationPlan).where(
                TemplateGenerationPlan.campaign_id == multi_bucket_cohort
            )
        )
        assert plan is not None
        assert plan.estimated_templates == 3
        assert plan.effective_limit is None
        assert plan.drafted_count == 3
        assert plan.policy_source == "default"


def test_effective_limit_caps_which_buckets_draft(multi_bucket_cohort: int) -> None:
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, come back to us.")] * 2)

    with SessionLocal() as session:
        set_campaign_policy(
            session,
            multi_bucket_cohort,
            max_templates=2,
            max_templates_pct=None,
            updated_by="test-manager",
        )
        session.commit()

    with SessionLocal() as session:
        outcome = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()

    assert outcome.estimated_templates == 3
    assert outcome.effective_limit == 2
    assert outcome.drafted_count == 2
    assert outcome.skipped_existing == 0
    assert [t.profile_key["product"] for t in outcome.templates] == EXPECTED_PRODUCT_ORDER[:2]

    with SessionLocal() as session:
        rows = session.scalars(
            select(MessageTemplate).where(MessageTemplate.campaign_id == multi_bucket_cohort)
        ).all()
    assert {row.profile_key["product"] for row in rows} == {"money market", "high yield"}


def test_raising_the_limit_and_redrafting_tops_up_without_duplicating(
    multi_bucket_cohort: int,
) -> None:
    first_llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, come back to us.")] * 2)
    with SessionLocal() as session:
        set_campaign_policy(
            session,
            multi_bucket_cohort,
            max_templates=2,
            max_templates_pct=None,
            updated_by="test-manager",
        )
        session.commit()
    with SessionLocal() as session:
        first = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=first_llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()
    assert first.drafted_count == 2

    second_llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, come back to us.")])
    with SessionLocal() as session:
        set_campaign_policy(
            session,
            multi_bucket_cohort,
            max_templates=None,
            max_templates_pct=None,
            updated_by="test-manager",
        )
        session.commit()
    with SessionLocal() as session:
        second = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=second_llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()

    assert second.estimated_templates == 3
    assert second.effective_limit is None
    assert second.skipped_existing == 2
    assert second.drafted_count == 1
    assert [t.profile_key["product"] for t in second.templates] == ["fixed income"]

    with SessionLocal() as session:
        rows = session.scalars(
            select(MessageTemplate).where(MessageTemplate.campaign_id == multi_bucket_cohort)
        ).all()
    assert len(rows) == 3  # no duplicates for the two buckets the first call already drafted


def test_a_guardrail_failure_leaves_nothing_to_skip_so_it_drafts_again(
    multi_bucket_cohort: int,
) -> None:
    """draft_template never persists a message_template row for a guardrail
    failure (see draft_template), so there is nothing for the skip logic to
    find -- the failed bucket is a plain candidate again on the next call.
    """
    first_llm = ScriptedLLMClient(
        [
            draft_json(body="Dear {{first_name}}, come back to us."),  # money market
            "not json at all",  # high yield, attempt 1
            "still not json",  # high yield, attempt 2 -- every retry fails
            draft_json(body="Dear {{first_name}}, come back to us."),  # fixed income
        ]
    )
    with SessionLocal() as session:
        first = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=first_llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()

    assert first.drafted_count == 2
    assert first.failed_guardrails == 1
    assert first.skipped_existing == 0

    second_llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, this time it lands.")])
    with SessionLocal() as session:
        second = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=second_llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()

    assert second.skipped_existing == 2
    assert second.drafted_count == 1
    assert [t.profile_key["product"] for t in second.templates] == ["high yield"]


def test_a_human_rejected_template_is_redrafted_not_skipped(multi_bucket_cohort: int) -> None:
    """Unlike a guardrail failure, a human rejection leaves a message_template
    row behind -- status='rejected' -- and §6.3 is explicit that only a
    *non*-rejected row counts as "already has a template".
    """
    llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, come back to us.")] * 3)
    with SessionLocal() as session:
        first = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()
    assert first.drafted_count == 3

    rejected_template_id = next(
        t.template_id for t in first.templates if t.profile_key["product"] == "fixed income"
    )
    with SessionLocal() as session:
        template = session.get(MessageTemplate, rejected_template_id)
        template.status = "rejected"
        session.commit()

    second_llm = ScriptedLLMClient([draft_json(body="Dear {{first_name}}, redrafted.")])
    with SessionLocal() as session:
        second = draft_templates_for_campaign(
            session,
            multi_bucket_cohort,
            settings=make_settings(),
            llm_client=second_llm,
            context_loader=_fixed_context_loader,
        )
        session.commit()

    # money market and high yield still stand (untouched); fixed income,
    # rejected, is the one candidate left, and it drafts again.
    assert second.skipped_existing == 2
    assert second.drafted_count == 1
    assert [t.profile_key["product"] for t in second.templates] == ["fixed income"]

    with SessionLocal() as session:
        rows = session.scalars(
            select(MessageTemplate).where(MessageTemplate.campaign_id == multi_bucket_cohort)
        ).all()
    fixed_income_rows = [r for r in rows if r.profile_key["product"] == "fixed income"]
    assert len(rows) == 4  # the original three plus the fresh fixed-income redraft
    assert len(fixed_income_rows) == 2  # the rejected one, kept, plus the redraft
