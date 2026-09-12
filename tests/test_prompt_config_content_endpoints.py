from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.prompt_config import build_voice_block
from app.db.models.prompt_config import FactEligibilityRule, PersonalizationPolicy, SafetyPolicy
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


def test_get_fact_fields_lists_model_fact_block_fields(
    configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    response = client.get("/api/v1/prompt-config/facts", headers=reviewer_1_headers)
    assert response.status_code == 200
    fields = response.json()
    assert "fund_name" in fields
    assert "typical_contribution_kes" in fields


def test_get_component_content_returns_draft_rows(
    configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    draft = client.post(
        "/api/v1/prompt-config/safety_policy/draft",
        json={"rows": [{"banned_words": ["park", "guarantee"]}]},
        headers=reviewer_1_headers,
    )
    assert draft.status_code == 200
    version = draft.json()["version"]

    content = client.get(
        f"/api/v1/prompt-config/safety_policy/{version}/content", headers=reviewer_1_headers
    )
    assert content.status_code == 200
    body = content.json()
    assert body["version"] == version
    assert body["rows"][0]["banned_words"] == ["park", "guarantee"]

    with SessionLocal() as session:
        session.execute(delete(SafetyPolicy).where(SafetyPolicy.version == version))
        session.commit()


def test_voice_draft_computes_rendered_text_from_structured_fields(
    configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    draft = client.post(
        "/api/v1/prompt-config/voice_contract/draft",
        json={
            "rows": [
                {
                    "persona": "A warm relationship manager",
                    "tone": "Warm, low pressure",
                }
            ]
        },
        headers=reviewer_1_headers,
    )
    assert draft.status_code == 200
    version = draft.json()["version"]

    content = client.get(
        f"/api/v1/prompt-config/voice_contract/{version}/content", headers=reviewer_1_headers
    )
    row = content.json()["rows"][0]
    expected = build_voice_block(persona="A warm relationship manager", tone="Warm, low pressure")
    assert row["rendered_text"] == expected
    assert expected

    from app.db.models.prompt_config import VoiceContract

    with SessionLocal() as session:
        session.execute(delete(VoiceContract).where(VoiceContract.version == version))
        session.commit()


def test_personalization_facts_round_trip_and_replace(
    configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    with SessionLocal() as session:
        session.add(PersonalizationPolicy(version=905, status="draft"))
        session.commit()

    try:
        put_response = client.put(
            "/api/v1/prompt-config/personalization/905/facts",
            json={"rows": [{"fact_field": "fund_name", "exposure_mode": "direct"}]},
            headers=reviewer_1_headers,
        )
        assert put_response.status_code == 200
        assert put_response.json() == [
            {"fact_field": "fund_name", "exposure_mode": "direct", "condition": None}
        ]

        get_response = client.get(
            "/api/v1/prompt-config/personalization/905/facts", headers=reviewer_1_headers
        )
        assert get_response.json() == [
            {"fact_field": "fund_name", "exposure_mode": "direct", "condition": None}
        ]

        replace_response = client.put(
            "/api/v1/prompt-config/personalization/905/facts",
            json={"rows": [{"fact_field": "fund_name", "exposure_mode": "placeholder"}]},
            headers=reviewer_1_headers,
        )
        assert replace_response.json() == [
            {"fact_field": "fund_name", "exposure_mode": "placeholder", "condition": None}
        ]
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(FactEligibilityRule).where(FactEligibilityRule.policy_version == 905)
            )
            session.execute(
                delete(PersonalizationPolicy).where(PersonalizationPolicy.version == 905)
            )
            session.commit()
