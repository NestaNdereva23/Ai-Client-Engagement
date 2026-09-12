from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import delete

from app.agents import email_agent
from app.agents.prompt_config import HARDCODED_CONFIGURATION, resolve_active_configuration
from app.config import Settings
from app.db.models.prompt_config import (
    FactEligibilityRule,
    PersonalizationPolicy,
)
from app.db.session import SessionLocal
from app.personalization.eligibility import FACT_FIELDS, PLACEHOLDER_FACT_FIELDS, PLACEHOLDER_FIELDS
from app.rules import versioning

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_prompt_config.py"


def _load_migrate_prompt_config():
    spec = importlib.util.spec_from_file_location("scripts_migrate_prompt_config", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hardcoded_settings() -> Settings:
    return Settings(prompt_config_source="hardcoded")


def _db_settings() -> Settings:
    return Settings(prompt_config_source="db")


def test_resolve_active_configuration_ignores_seeded_data_when_hardcoded(db: None) -> None:
    with SessionLocal() as session:
        config = resolve_active_configuration(
            session, angle="fee_warning", tier=None, settings=_hardcoded_settings()
        )

    assert config == HARDCODED_CONFIGURATION
    assert config.voice_text is None
    assert config.safety_words is None


def test_resolve_active_configuration_reads_the_db_when_flag_is_db(db: None) -> None:
    with SessionLocal() as session:
        config = resolve_active_configuration(
            session, angle="fee_warning", tier=None, settings=_db_settings()
        )

    assert config.voice_contract_version == 1
    assert config.voice_text == email_agent._BASE_INSTRUCTIONS_CORE


def test_build_seed_voice_and_safety_match_current_source_constants() -> None:
    module = _load_migrate_prompt_config()
    seed = module.build_seed()

    assert seed["voice_contract"] == [
        {
            "body_markdown": email_agent._BASE_INSTRUCTIONS_CORE,
            "rendered_text": email_agent._BASE_INSTRUCTIONS_CORE,
        }
    ]
    assert seed["safety_policy"][0]["banned_words"] == list(email_agent.BANNED_WORDS)
    assert seed["safety_policy"][0]["campaign_prohibitions"] == list(
        email_agent.CAMPAIGN_PROHIBITIONS
    )
    assert seed["output_policy"][0]["placeholder_rules"]["fields"] == list(PLACEHOLDER_FACT_FIELDS)


def test_personalization_eligibility_rows_mark_placeholder_fields(db: None) -> None:
    module = _load_migrate_prompt_config()
    rows = module.personalization_eligibility_rows()

    by_field = {row["fact_field"]: row["exposure_mode"] for row in rows}
    assert set(by_field) == set(FACT_FIELDS)
    for field in PLACEHOLDER_FIELDS:
        assert by_field[field] == "placeholder"
    assert by_field["fund_name"] == "direct"


def test_apply_seed_writes_drafts_not_publications(db: None) -> None:
    module = _load_migrate_prompt_config()
    versions = module.apply_seed(by="test-migrate-prompt-config")

    try:
        with SessionLocal() as session:
            for component_type in ("voice_contract", "safety_policy", "output_policy"):
                rows = versioning._rows_for(
                    session,
                    versioning.COMPONENTS[component_type],
                    versioning.DEFAULT_COMPONENT_KEY,
                    versions[component_type],
                )
                assert rows
                assert all(row.status == "draft" for row in rows)

            eligibility_rows = session.scalars(
                FactEligibilityRule.__table__.select().where(
                    FactEligibilityRule.policy_version == versions["personalization_policy"]
                )
            ).all()
            assert len(eligibility_rows) == len(FACT_FIELDS)
    finally:
        with SessionLocal() as session:
            for component_type in ("voice_contract", "safety_policy", "output_policy"):
                session.execute(
                    delete(versioning.COMPONENTS[component_type].model).where(
                        versioning.COMPONENTS[component_type].model.version
                        == versions[component_type],
                        versioning.COMPONENTS[component_type].model.status == "draft",
                    )
                )
            session.execute(
                delete(FactEligibilityRule).where(
                    FactEligibilityRule.policy_version == versions["personalization_policy"]
                )
            )
            session.execute(
                delete(PersonalizationPolicy).where(
                    PersonalizationPolicy.version == versions["personalization_policy"],
                    PersonalizationPolicy.status == "draft",
                )
            )
            session.commit()
