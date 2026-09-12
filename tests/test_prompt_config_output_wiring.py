from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.agents.email_agent import ALLOWED_PLACEHOLDERS, resolve_allowed_placeholders
from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.prompt_config import (
    FactEligibilityRule,
    OutputPolicy,
    PersonalizationPolicy,
    VoiceContract,
)
from app.db.models.rag import RagSetting
from app.db.models.rules import MessageAngleCatalog
from app.db.session import SessionLocal
from app.main import app
from app.services.prompt_testing import generate_test_draft
from app.services.rag import get_rag_enabled, set_rag_enabled

client = TestClient(app)


class ScriptedLLMClient:
    model = "scripted"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_usage = None
        self.last_system: str | None = None

    def generate(self, *, system: str, user: str) -> str:
        self.last_system = system
        return self._reply


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


def test_resolve_allowed_placeholders_falls_back_to_hardcoded_default() -> None:
    assert resolve_allowed_placeholders(None) == ALLOWED_PLACEHOLDERS


def test_resolve_allowed_placeholders_uses_the_given_fields() -> None:
    resolved = resolve_allowed_placeholders(["typical_contribution"])
    assert resolved == ("{{first_name}}", "{{fund_name}}", "{{typical_contribution}}")


@pytest.fixture
def voice_row(db: None):
    with SessionLocal() as session:
        row = VoiceContract(
            version=901,
            status="published",
            body_markdown="Base voice instructions.",
            rendered_text="Base voice instructions.",
            default_sign_off="Warm regards, Cytonn Client Success",
        )
        session.add(row)
        session.commit()
        version = row.version

    yield version

    with SessionLocal() as session:
        session.execute(delete(VoiceContract).where(VoiceContract.version == version))
        session.commit()


def test_default_sign_off_reaches_the_prompt_when_no_tier_is_pinned(voice_row: int) -> None:
    with SessionLocal() as session:
        llm_client = ScriptedLLMClient('{"subject": "s", "body": "Hi {{first_name}}, b"}')
        generate_test_draft(
            session,
            angle=None,
            tier=None,
            angle_version=None,
            tier_version=None,
            voice_version=voice_row,
            safety_version=None,
            output_version=None,
            personalization_version=None,
            fact_profile={"fund_name": "Cytonn Money Market Fund"},
            settings=make_settings(),
            llm_client=llm_client,
        )
        assert "Warm regards, Cytonn Client Success" in llm_client.last_system


@pytest.fixture
def output_row(db: None):
    with SessionLocal() as session:
        row = OutputPolicy(
            version=902,
            status="published",
            placeholder_rules={"fields": ["typical_contribution"]},
        )
        session.add(row)
        session.commit()
        version = row.version

    yield version

    with SessionLocal() as session:
        session.execute(delete(OutputPolicy).where(OutputPolicy.version == version))
        session.commit()


def test_output_policy_rejects_a_placeholder_outside_its_own_field_list(
    output_row: int,
) -> None:
    with SessionLocal() as session:
        llm_client = ScriptedLLMClient(
            '{"subject": "s", "body": "Hi {{first_name}}, {{years_since_exit}}"}'
        )
        runs = generate_test_draft(
            session,
            angle=None,
            tier=None,
            angle_version=None,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=output_row,
            personalization_version=None,
            fact_profile={"fund_name": "Cytonn Money Market Fund"},
            settings=make_settings(),
            llm_client=llm_client,
        )
        assert runs[0].status == "rejected"
        assert runs[0].failed_guardrail == "structured_output"


def test_output_policy_accepts_a_placeholder_inside_its_own_field_list(output_row: int) -> None:
    with SessionLocal() as session:
        llm_client = ScriptedLLMClient(
            '{"subject": "s", "body": "Hi {{first_name}}, {{typical_contribution}} is on file. '
            'Best regards, Relationship Manager"}'
        )
        runs = generate_test_draft(
            session,
            angle=None,
            tier=None,
            angle_version=None,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=output_row,
            personalization_version=None,
            fact_profile={"fund_name": "Cytonn Money Market Fund"},
            settings=make_settings(),
            llm_client=llm_client,
        )
        assert runs[0].failed_guardrail != "structured_output"


@pytest.fixture
def personalization_row(db: None):
    with SessionLocal() as session:
        session.add(PersonalizationPolicy(version=903, status="published"))
        session.commit()
        session.add(
            FactEligibilityRule(
                policy_version=903, angle=None, fact_field="fund_name", exposure_mode="direct"
            )
        )
        session.add(
            FactEligibilityRule(
                policy_version=903,
                angle=None,
                fact_field="typical_contribution_kes",
                exposure_mode="placeholder",
            )
        )
        session.commit()
        version = 903

    yield version

    with SessionLocal() as session:
        session.execute(
            delete(FactEligibilityRule).where(FactEligibilityRule.policy_version == 903)
        )
        session.execute(delete(PersonalizationPolicy).where(PersonalizationPolicy.version == 903))
        session.commit()


def test_personalization_eligibility_filters_facts_reaching_the_model(
    personalization_row: int,
) -> None:
    run_id = None
    with SessionLocal() as session:
        llm_client = ScriptedLLMClient(
            '{"subject": "s", "body": "Hi {{first_name}}, {{typical_contribution}}. '
            'Best regards, Relationship Manager"}'
        )
        runs = generate_test_draft(
            session,
            angle=None,
            tier=None,
            angle_version=None,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=None,
            personalization_version=personalization_row,
            fact_profile={
                "fund_name": "Cytonn Money Market Fund",
                "typical_contribution_kes": 15000,
                "exit_reason": "charge_settled",
            },
            settings=make_settings(),
            llm_client=llm_client,
        )
        session.commit()
        run_id = runs[0].run_id
        payload = runs[0].context_payload

    assert payload is not None
    assert payload["fund_name"] == "Cytonn Money Market Fund"
    assert payload["typical_contribution_kes"] == 15000
    assert "exit_reason" not in payload

    with SessionLocal() as session:
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.commit()


@pytest.fixture
def cadence_dependent_personalization_row(db: None):
    with SessionLocal() as session:
        session.add(PersonalizationPolicy(version=906, status="published"))
        session.commit()
        session.add(
            FactEligibilityRule(
                policy_version=906,
                angle=None,
                fact_field="fund_name",
                exposure_mode="direct",
            )
        )
        session.add(
            FactEligibilityRule(
                policy_version=906,
                angle=None,
                fact_field="invested_every_n_days",
                exposure_mode="direct",
            )
        )
        session.commit()
        version = 906

    yield version

    with SessionLocal() as session:
        session.execute(
            delete(FactEligibilityRule).where(FactEligibilityRule.policy_version == 906)
        )
        session.execute(delete(PersonalizationPolicy).where(PersonalizationPolicy.version == 906))
        session.commit()


def test_hiding_cadence_band_also_drops_its_dependent_cadence_interval(
    cadence_dependent_personalization_row: int,
) -> None:
    run_id = None
    with SessionLocal() as session:
        llm_client = ScriptedLLMClient('{"subject": "s", "body": "Hi {{first_name}}, b"}')
        runs = generate_test_draft(
            session,
            angle=None,
            tier=None,
            angle_version=None,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=None,
            personalization_version=cadence_dependent_personalization_row,
            fact_profile={
                "fund_name": "Cytonn Money Market Fund",
                "cadence_band": "Regular",
                "invested_every_n_days": 21,
            },
            settings=make_settings(),
            llm_client=llm_client,
        )
        session.commit()
        run_id = runs[0].run_id
        payload = runs[0].context_payload

    assert runs[0].failed_guardrail is None
    assert payload is not None
    assert payload["fund_name"] == "Cytonn Money Market Fund"
    assert "cadence_band" not in payload
    assert "invested_every_n_days" not in payload

    with SessionLocal() as session:
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.commit()


@pytest.fixture
def cta_angle(db: None):
    angle = "cta_wiring_fixture_angle"
    with SessionLocal() as session:
        session.add(
            MessageAngleCatalog(
                version=904,
                angle=angle,
                headline="CTA fixture",
                who="fixture clients",
                claim="fixture claim",
                ask="fixture ask",
                never="fixture never",
                cta="Original CTA phrase",
                status="published",
            )
        )
        session.commit()

    yield angle

    with SessionLocal() as session:
        session.execute(
            delete(MessageAngleCatalog).where(
                MessageAngleCatalog.angle == angle, MessageAngleCatalog.version == 904
            )
        )
        session.commit()


def test_cta_override_reaches_the_prompt_without_touching_the_catalog_row(
    cta_angle: str,
) -> None:
    with SessionLocal() as session:
        llm_client = ScriptedLLMClient('{"subject": "s", "body": "Hi {{first_name}}, b"}')
        generate_test_draft(
            session,
            angle=cta_angle,
            tier=None,
            angle_version=904,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=None,
            personalization_version=None,
            fact_profile={"fund_name": "Cytonn Money Market Fund"},
            settings=make_settings(),
            llm_client=llm_client,
            cta_override="Overridden CTA phrase",
        )
        assert "Overridden CTA phrase" in llm_client.last_system
        assert "Original CTA phrase" not in llm_client.last_system

    with SessionLocal() as session:
        row = session.scalar(
            select(MessageAngleCatalog).where(
                MessageAngleCatalog.angle == cta_angle, MessageAngleCatalog.version == 904
            )
        )
        assert row.cta == "Original CTA phrase"


def test_use_rag_toggle_controls_whether_retrieved_facts_reach_the_prompt(
    cta_angle: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeChunk:
        chunk_id = -1
        text = "SENTINEL_RAG_FACT_FOR_TESTING"

    monkeypatch.setattr(
        "app.services.prompt_testing.retrieve_product_facts", lambda *a, **k: (FakeChunk(),)
    )

    with SessionLocal() as session:
        on_client = ScriptedLLMClient('{"subject": "s", "body": "Hi {{first_name}}, b"}')
        generate_test_draft(
            session,
            angle=cta_angle,
            tier=None,
            angle_version=904,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=None,
            personalization_version=None,
            fact_profile={"fund_name": "Cytonn Money Market Fund"},
            settings=make_settings(),
            llm_client=on_client,
            use_rag=True,
        )
        assert "SENTINEL_RAG_FACT_FOR_TESTING" in on_client.last_system

        off_client = ScriptedLLMClient('{"subject": "s", "body": "Hi {{first_name}}, b"}')
        generate_test_draft(
            session,
            angle=cta_angle,
            tier=None,
            angle_version=904,
            tier_version=None,
            voice_version=None,
            safety_version=None,
            output_version=None,
            personalization_version=None,
            fact_profile={"fund_name": "Cytonn Money Market Fund"},
            settings=make_settings(),
            llm_client=off_client,
            use_rag=False,
        )
        assert "SENTINEL_RAG_FACT_FOR_TESTING" not in off_client.last_system


def test_rag_enabled_service_round_trips(db: None) -> None:
    with SessionLocal() as session:
        original = get_rag_enabled(session)
        session.commit()

    try:
        with SessionLocal() as session:
            set_rag_enabled(session, False)
            session.commit()
        with SessionLocal() as session:
            assert get_rag_enabled(session) is False

        with SessionLocal() as session:
            set_rag_enabled(session, True)
            session.commit()
        with SessionLocal() as session:
            assert get_rag_enabled(session) is True
    finally:
        with SessionLocal() as session:
            set_rag_enabled(session, original)
            session.commit()


def test_rag_settings_endpoints(
    configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    original = client.get("/api/v1/rag/settings", headers=reviewer_1_headers).json()["enabled"]
    try:
        response = client.put(
            "/api/v1/rag/settings", json={"enabled": False}, headers=reviewer_1_headers
        )
        assert response.status_code == 200
        assert response.json() == {"enabled": False}

        response = client.get("/api/v1/rag/settings", headers=reviewer_1_headers)
        assert response.json() == {"enabled": False}
    finally:
        client.put("/api/v1/rag/settings", json={"enabled": original}, headers=reviewer_1_headers)


def test_rag_setting_row_cleanup_is_not_needed_between_runs(db: None) -> None:
    with SessionLocal() as session:
        row = session.get(RagSetting, 1)
        assert row is not None
