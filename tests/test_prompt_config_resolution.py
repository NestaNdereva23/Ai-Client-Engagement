from __future__ import annotations

import functools
import json
from datetime import date
from uuid import uuid4

import pytest

from app.agents import email_agent
from app.agents.email_agent import CAMPAIGN_PROHIBITIONS, build_system_prompt
from app.agents.email_channel import EmailAgent
from app.agents.graph import ClientContext
from app.agents.prompt_config import build_voice_block, resolve_active_configuration
from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.prompt_config import OutputPolicy, SafetyPolicy, VoiceContract
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.personalization.eligibility import PLACEHOLDER_FACT_FIELDS

POLICY_VERSION = 1
IN_FORCE = date(2026, 9, 11)


def test_build_voice_block_prefers_body_markdown() -> None:
    text = build_voice_block(body_markdown="the uploaded text", persona="warm")
    assert text == "the uploaded text"


def test_build_voice_block_assembles_structured_fields_in_order() -> None:
    text = build_voice_block(
        persona="A relationship manager",
        tone="Warm and low pressure",
        readability_guidance="Short sentences",
    )
    assert text == "A relationship manager\n\nWarm and low pressure\n\nShort sentences"


def test_build_voice_block_with_nothing_given_is_empty() -> None:
    assert build_voice_block() == ""


def test_migration_seed_matches_the_source_it_was_copied_from(db: None) -> None:
    with SessionLocal() as session:
        voice = session.query(VoiceContract).filter_by(version=POLICY_VERSION).one()
        safety = session.query(SafetyPolicy).filter_by(version=POLICY_VERSION).one()
        output = session.query(OutputPolicy).filter_by(version=POLICY_VERSION).one()

    assert voice.body_markdown == email_agent._BASE_INSTRUCTIONS_CORE
    assert voice.rendered_text == voice.body_markdown
    assert tuple(safety.banned_words) == email_agent.BANNED_WORDS
    assert tuple(safety.campaign_prohibitions) == CAMPAIGN_PROHIBITIONS
    assert output.placeholder_rules["fields"] == list(PLACEHOLDER_FACT_FIELDS)


def test_resolve_active_configuration_returns_v1_for_now(db: None) -> None:
    with SessionLocal() as session:
        config = resolve_active_configuration(session, angle="fee_warning", tier=None)

    assert config.voice_contract_version == POLICY_VERSION
    assert config.safety_policy_version == POLICY_VERSION
    assert config.output_policy_version == POLICY_VERSION
    assert config.personalization_policy_version == POLICY_VERSION
    assert config.tier_contract_version is None
    assert config.voice_text == email_agent._BASE_INSTRUCTIONS_CORE
    assert config.safety_words == email_agent.BANNED_WORDS
    assert config.campaign_prohibitions == CAMPAIGN_PROHIBITIONS


def test_resolve_active_configuration_resolves_the_tier_contract_version(db: None) -> None:
    with SessionLocal() as session:
        config = resolve_active_configuration(session, angle=None, tier="T1")

    assert config.tier_contract_version is not None


def test_resolve_active_configuration_returns_none_for_an_unpublished_date(db: None) -> None:
    with SessionLocal() as session:
        config = resolve_active_configuration(session, angle=None, tier=None, at=date(2020, 1, 1))

    assert config.voice_contract_version is None
    assert config.voice_text is None


@pytest.mark.parametrize(
    ("angle", "tier"),
    [
        ("fee_warning", "T1"),
        ("not_a_goodbye", "T2"),
        ("sitting_still", None),
        (None, None),
    ],
)
def test_v1_db_driven_prompt_matches_the_hardcoded_prompt_byte_for_byte(
    db: None, angle: str | None, tier: str | None
) -> None:
    facts = {"fund_name": "Cytonn Money Market Fund", "typical_contribution_kes": 20000}

    with SessionLocal() as session:
        config = resolve_active_configuration(session, angle=angle, tier=tier)

    from_db = build_system_prompt(
        angle=angle,
        prompt_variant=None,
        facts=facts,
        voice_text=config.voice_text,
        safety_words=config.safety_words,
        safety_phrases=config.safety_phrases,
        campaign_prohibitions=config.campaign_prohibitions,
        output_rules=config.output_rules,
    )
    hardcoded = build_system_prompt(angle=angle, prompt_variant=None, facts=facts)

    assert from_db == hardcoded


@pytest.fixture
def client(db: None):
    fund_id = 952
    client_id = 95201
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

    yield client_id

    with SessionLocal() as session:
        session.query(GenerationRun).filter_by(client_id=client_id).delete()
        session.query(Clients).filter_by(client_id=client_id).delete()
        session.query(Funds).filter_by(unit_fund_id=fund_id).delete()
        session.commit()


def test_persist_generation_run_stamps_every_resolved_version_and_context_payload(
    client: int,
) -> None:
    state = {
        "run_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "client_id": client,
        "product": "money market",
        "angle": "fee_warning",
        "priority_tier": "T1",
        "prompt_variant": "fee_warning",
        "rule_version": 3,
        "angle_catalog_version": 2,
        "tier_contract_version": 4,
        "voice_contract_version": 1,
        "safety_policy_version": 1,
        "output_policy_version": 1,
        "personalization_policy_version": 1,
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {"subject": "s", "body": "b"},
        "context": {"fund_name": "Cytonn Money Market Fund"},
    }
    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)

    assert stored.tier_contract_version == 4
    assert stored.voice_contract_version == 1
    assert stored.safety_policy_version == 1
    assert stored.output_policy_version == 1
    assert stored.personalization_policy_version == 1
    assert stored.context_payload == {"fund_name": "Cytonn Money Market Fund"}


def test_persist_generation_run_leaves_the_new_stamps_null_without_them(client: int) -> None:
    state = {
        "run_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "client_id": client,
        "product": "money market",
        "angle": "fee_warning",
        "priority_tier": "T1",
        "prompt_variant": "fee_warning",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {"subject": "s", "body": "b"},
    }
    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )

    with SessionLocal() as session:
        run = persist_generation_run(session, state, settings)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)

    assert stored.tier_contract_version is None
    assert stored.voice_contract_version is None
    assert stored.context_payload is None


class ScriptedLLMClient:
    model = "scripted"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_usage = None

    def generate(self, *, system: str, user: str) -> str:
        self.seen_system = system
        return self._reply


def test_the_live_graph_stamps_the_resolved_versions_via_config_resolver(db: None) -> None:
    def context_loader(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={},
            angle="fee_warning",
            prompt_variant="fee_warning",
            chunks=(),
            facts={"fund_name": "Cytonn Money Market Fund"},
            priority_tier="T1",
        )

    reply = json.dumps(
        {
            "subject": "A quick note",
            "body": (
                "Hi {{first_name}}, your Cytonn Money Market Fund is still here.\n\n"
                "Best regards, Relationship Manager"
            ),
        }
    )

    with SessionLocal() as session:
        agent = EmailAgent(
            context_loader=context_loader,
            llm_client=ScriptedLLMClient(reply),
            config_resolver=functools.partial(resolve_active_configuration, session),
        )
        state = agent.generate(client_id=1, product="money market")

    assert state["voice_contract_version"] == POLICY_VERSION
    assert state["safety_policy_version"] == POLICY_VERSION
    assert state["output_policy_version"] == POLICY_VERSION
    assert state["personalization_policy_version"] == POLICY_VERSION
    assert email_agent._BASE_INSTRUCTIONS_CORE in state["system_prompt"]
