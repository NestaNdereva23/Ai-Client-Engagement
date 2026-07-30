"""Server-side re-attachment: placeholder substitution and outreach_message creation.

The substitution and name-fallback logic is pure and tested without a
database. create_outreach_message itself needs the restricted role from the
PII boundary migration, so those tests skip when it is absent, the same way
test_db_roles.py does.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.services.review import (
    _fetch_client_name,
    _first_name_from_full_name,
    create_outreach_message,
    personalize_content,
    resolve_placeholders,
)


def test_first_name_from_full_name_uses_the_first_word() -> None:
    assert _first_name_from_full_name("Jane Doe") == "Jane"


def test_first_name_from_full_name_falls_back_when_none() -> None:
    assert _first_name_from_full_name(None) == "Valued Client"


def test_first_name_from_full_name_falls_back_when_blank() -> None:
    assert _first_name_from_full_name("   ") == "Valued Client"


def test_resolve_placeholders_substitutes_both_tokens() -> None:
    result = resolve_placeholders(
        "Dear {{first_name}}, your {{fund_name}} awaits.",
        first_name="Jane",
        fund_name="Money Market Fund",
    )
    assert result == "Dear Jane, your Money Market Fund awaits."


def test_resolve_placeholders_leaves_text_without_placeholders_unchanged() -> None:
    assert resolve_placeholders("no tokens here", first_name="Jane", fund_name="MMF") == (
        "no tokens here"
    )


def test_personalize_content_applies_to_subject_and_body() -> None:
    draft = {"subject": "Hi {{first_name}}", "body": "About {{fund_name}}, {{first_name}}."}
    result = personalize_content(draft, first_name="Jane", fund_name="MMF")
    assert result == {"subject": "Hi Jane", "body": "About MMF, Jane."}


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def accepted_state(client_id: int, **overrides) -> dict:
    state = {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "winback_habit",
        "prompt_variant": "habit_premium",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
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
def scenario(roles):
    """A fund, a named client, a vault row, an accepted run, and a campaign."""
    fund_id = 970
    client_id = 97001
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
        session.commit()
        run = persist_generation_run(session, accepted_state(client_id), make_settings())
        campaign = Campaign(name="test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        run_id = run.run_id

    yield client_id, run_id, campaign_id, fund_id

    with SessionLocal() as session:
        session.execute(delete(OutreachMessage).where(OutreachMessage.generation_run_id == run_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_fetch_client_name_reads_the_vault_under_the_restricted_role(scenario) -> None:
    client_id, *_ = scenario
    assert _fetch_client_name(client_id) == "Jane Doe"


def test_create_outreach_message_writes_personalized_content_with_real_values(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert stored.personalized_content == {
            "subject": "Come back to Cytonn Money Market Fund",
            "body": "Dear Jane, we miss you.",
        }


def test_create_outreach_message_copies_ai_draft_content_unchanged(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert stored.ai_draft_content == {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        }


def test_create_outreach_message_falls_back_when_the_vault_has_no_name(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        session.get(PiiVault, client_id).client_name = None
        session.commit()

    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert "Valued Client" in stored.personalized_content["body"]
