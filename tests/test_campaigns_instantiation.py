"""Instantiation: approved template + one client's own facts -> outreach_message.

instantiate_message covers the per-client core: approval gating, placeholder
resolution, and the post-instantiation guardrail re-check. instantiate_template
covers the orchestration: matching, touching, and no double-instantiation.
"""

from __future__ import annotations

import uuid
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.agents.graph import ClientContext
from app.campaigns.enrollment import enroll_cohort
from app.campaigns.instantiation import instantiate_template
from app.config import Settings, get_settings
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.message_template import MessageTemplate
from app.db.models.models import ClientFeatures, ClientFund, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.rules import TierContract
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.rules.tier_contract import TierSpec, save_tier_contract_version
from app.services.campaigns import add_campaign_step
from app.services.review import TemplateNotApproved, instantiate_message

FUND_ID = 9720
CLIENT_ID = 972001

DRAFT_BODY = (
    "Dear {{first_name}}, your typical contribution was {{typical_contribution}}, "
    "largest {{largest_contribution}}, held {{years_since_exit}} years, "
    "{{days_held_after_last_topup}} days after top-up, left {{month_they_left}}, "
    "cadence {{cadence_interval_days}} days."
)


def make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


def accepted_state(client_id: int, **overrides) -> dict:
    state = {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "pick_up_again",
        "priority_tier": "T3",
        "prompt_variant": "pick_up_again",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": DRAFT_BODY,
        },
    }
    state.update(overrides)
    return state


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


@pytest.fixture
def client(roles):
    """A fund, a named client, and real band/numeric facts via client_features
    and client_fund directly, skipping the full ingestion pipeline."""
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                n_purchases_returned=1,
                n_sales_returned=1,
            )
        )
        session.commit()
        session.add(
            PiiVault(client_id=CLIENT_ID, client_name="Jane Doe", contact_email="jane@example.com")
        )
        session.add(
            ClientFeatures(
                client_id=CLIENT_ID,
                fund_type="money_market",
                cadence_band="Regular",
                purchase_depth="single",
            )
        )
        session.commit()
        session.add(
            ClientFund(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                client_code="C-972001",
                n_purchases=3,
                n_sales=1,
                is_primary_contact_row=True,
                avg_ticket=5000,
                max_ticket=20000,
                rhythm_days=30,
                hold_days=45,
                days_cold=200,
                exit_date=date(2025, 3, 15),
            )
        )
        session.commit()

    yield CLIENT_ID

    with SessionLocal() as session:
        session.execute(delete(ClientFund).where(ClientFund.client_id == CLIENT_ID))
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == CLIENT_ID))
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


@pytest.fixture
def campaign(client: int):
    with SessionLocal() as session:
        row = Campaign(name="instantiation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id
        add_campaign_step(session, campaign_id, offset_days=0, message_angle="pick_up_again")
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        # run's teardown clears message-shaped rows first.
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def run(client: int):
    with SessionLocal() as session:
        row = persist_generation_run(session, accepted_state(client), make_settings())
        session.commit()
        run_id = row.run_id

    yield run_id

    with SessionLocal() as session:
        # Every outreach_message this test produced shares this run_id,
        # whatever path made it.
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.generation_run_id == run_id)
        ).all()
        if message_ids:
            session.execute(delete(TouchLog).where(TouchLog.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
        session.execute(delete(MessageTemplate).where(MessageTemplate.generation_run_id == run_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.commit()


def make_template(
    campaign_id: int, run_id: str, *, status: str = "approved", body: str = DRAFT_BODY
):
    return MessageTemplate(
        template_id=uuid.uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run_id,
        profile_key={
            "message_angle": "pick_up_again",
            "priority_tier": "T3",
            "product": "money market",
            "has_cadence": True,
            "stale_contact": False,
            "exit_reason_charge_settled": False,
            "fund_name_known": False,
        },
        ai_draft_content={"subject": "Come back to {{fund_name}}", "body": body},
        status=status,
    )


def test_instantiate_message_raises_when_the_template_is_not_approved(
    campaign: int, run: str, client: int
) -> None:
    with SessionLocal() as session:
        template = make_template(campaign, run, status="pending_review")
        session.add(template)
        session.commit()

        with pytest.raises(TemplateNotApproved):
            instantiate_message(session, template, client, campaign_id=campaign)


def test_instantiate_message_fills_every_placeholder_with_this_clients_own_figures(
    campaign: int, run: str, client: int
) -> None:
    with SessionLocal() as session:
        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        message = instantiate_message(session, template, client, campaign_id=campaign)
        session.commit()

    assert message is not None
    body = message.personalized_content["body"]
    assert "Dear Jane," in body
    assert "typical contribution was 5,000" in body
    assert "largest 20,000" in body
    assert "held 0.5 years" in body
    assert "45 days after top-up" in body
    assert "left March 2025" in body
    assert "cadence 30 days" in body
    assert "{{" not in body
    assert message.template_id == template.template_id
    assert message.generation_run_id == template.generation_run_id
    assert message.status == "pending_review"
    assert message.call_brief is None


def test_instantiate_message_builds_a_call_brief_when_the_tiers_contract_calls_for_one(
    campaign: int, run: str, client: int
) -> None:
    """call_brief is rendered at instantiation from this client's own real
    facts, never from the bucket's shared placeholder draft."""
    contract_version = 97300002
    with SessionLocal() as session:
        save_tier_contract_version(
            session,
            contract_version,
            [
                TierSpec(
                    tier="T3",
                    display_name="Tier 3",
                    primary_channel="email",
                    secondary_channel="call_brief",
                    max_words=120,
                    sign_off="Client Services",
                    human_approval=True,
                    review_sample_rate=1.0,
                )
            ],
            # Today, not some far-back date: the seeded contract is in
            # force from today too, and a tie on valid_from is broken by
            # the higher version, which is this one.
            valid_from=date.today(),
        )
        session.commit()

        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        message = instantiate_message(session, template, client, campaign_id=campaign)
        session.commit()

    try:
        assert message is not None
        assert message.call_brief is not None
        assert "Call brief:" in message.call_brief
        assert "Call as: Client Services." in message.call_brief
        # The client's real, unformatted facts -- not the bucket's
        # placeholder tokens, and not this client's own name (render_call_brief
        # never takes one).
        assert "typical_contribution_kes: 5000" in message.call_brief
        assert "{{" not in message.call_brief
        assert "Jane" not in message.call_brief
    finally:
        with SessionLocal() as session:
            session.execute(delete(TierContract).where(TierContract.version == contract_version))
            session.commit()


def test_instantiate_message_auto_approves_when_the_tiers_sampling_says_so(
    campaign: int, run: str, client: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tier_sampling_enabled and the tier's review_sample_rate together
    decide an instance's status; the template's own review is untouched."""
    contract_version = 97300001
    monkeypatch.setenv("TIER_SAMPLING_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr("app.rules.tier_contract.random.random", lambda: 0.9)

    with SessionLocal() as session:
        save_tier_contract_version(
            session,
            contract_version,
            [
                TierSpec(
                    tier="T3",
                    display_name="Tier 3",
                    primary_channel="email",
                    max_words=120,
                    sign_off="Client Services",
                    human_approval=False,
                    review_sample_rate=0.5,
                )
            ],
            # Today, not some far-back date: the seeded contract is in
            # force from today too, and a tie on valid_from is broken by
            # the higher version, which is this one.
            valid_from=date.today(),
        )
        session.commit()

        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        message = instantiate_message(session, template, client, campaign_id=campaign)
        session.commit()

    try:
        assert message is not None
        assert message.status == "approved"
    finally:
        get_settings.cache_clear()
        with SessionLocal() as session:
            session.execute(delete(TierContract).where(TierContract.version == contract_version))
            session.commit()


def test_instantiate_message_rejects_a_client_with_nothing_to_fill_a_token_with(
    campaign: int, run: str, client: int
) -> None:
    """A client with no numeric row at all leaves a placeholder-filled token
    unresolved; check_no_unresolved_placeholders catches it."""
    with SessionLocal() as session:
        session.execute(delete(ClientFund).where(ClientFund.client_id == client))
        session.commit()

        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        message = instantiate_message(session, template, client, campaign_id=campaign)
        session.commit()

        assert message is None
        assert (
            session.scalars(
                select(OutreachMessage.message_id).where(
                    OutreachMessage.generation_run_id == template.generation_run_id
                )
            ).all()
            == []
        )


def make_matching_context_loader():
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={},
            angle="pick_up_again",
            prompt_variant="pick_up_again",
            priority_tier="T3",
            chunks=(),
            facts={"invested_every_n_days": 30},
        )

    return load


def make_non_matching_context_loader():
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={},
            angle="not_a_goodbye",
            prompt_variant="not_a_goodbye",
            priority_tier="T3",
            chunks=(),
            facts={"invested_every_n_days": 30},
        )

    return load


def test_instantiate_template_creates_a_message_for_a_matching_due_client(
    campaign: int, run: str, client: int
) -> None:
    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client])
        session.commit()
        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        messages = instantiate_template(
            session,
            template,
            campaign_id=campaign,
            context_loader=make_matching_context_loader(),
        )
        session.commit()

    assert len(messages) == 1
    assert messages[0].client_id == client
    assert messages[0].template_id == template.template_id

    with SessionLocal() as session:
        touch = session.scalars(
            select(TouchLog).where(TouchLog.enrollment_id.in_(select(Enrollment.enrollment_id)))
        ).first()
        assert touch is not None
        assert touch.message_id == messages[0].message_id


def test_instantiate_template_skips_a_client_whose_profile_does_not_match(
    campaign: int, run: str, client: int
) -> None:
    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client])
        session.commit()
        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        messages = instantiate_template(
            session,
            template,
            campaign_id=campaign,
            context_loader=make_non_matching_context_loader(),
        )
        session.commit()

        assert messages == []


def test_instantiate_template_does_not_double_instantiate_on_a_second_call(
    campaign: int, run: str, client: int
) -> None:
    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client])
        session.commit()
        template = make_template(campaign, run)
        session.add(template)
        session.commit()

        first = instantiate_template(
            session,
            template,
            campaign_id=campaign,
            context_loader=make_matching_context_loader(),
        )
        session.commit()
        second = instantiate_template(
            session,
            template,
            campaign_id=campaign,
            context_loader=make_matching_context_loader(),
        )
        session.commit()

    assert len(first) == 1
    assert second == []
