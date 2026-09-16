from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.agent import SituationActionMapping
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

AS_OF = "2026-09-15"

BASE = "/api/v1/agent-studio"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_scenario_with_full_threshold_overrides_needs_no_live_config() -> None:
    response = client.post(
        f"{BASE}/scenario",
        json={
            "balance": 500_000.0,
            "risk_band": "Low",
            "funds_held": 1,
            "new_client_days": 30,
            "months_until_empty_threshold": 6.0,
            "small_balance_kes": 100.0,
            "awaiting_call_days": 2,
            "money_ceiling_kes": 250_000.0,
            "as_of": AS_OF,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["winner"] == "healthy_one_fund"
    assert body["outcome"] == "needs_approval"


def test_scenario_matching_nothing_reports_no_situation() -> None:
    response = client.post(
        f"{BASE}/scenario",
        json={
            "balance": 200_000.0,
            "risk_band": "High",
            "funds_held": 3,
            "new_client_days": 30,
            "months_until_empty_threshold": 6.0,
            "small_balance_kes": 100.0,
            "awaiting_call_days": 2,
            "as_of": AS_OF,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["winner"] is None
    assert body["outcome"] == "no_situation_matched"


def test_batch_simulation_returns_a_row_per_situation() -> None:
    response = client.get(f"{BASE}/batch-simulation", params={"as_of": AS_OF})

    assert response.status_code == 200
    body = response.json()
    situations = {row["situation"] for row in body["by_situation"]}
    assert situations == {
        "signed_up_recently",
        "fee_pressure_gone_quiet",
        "fee_pressure_active_contributor",
        "very_small_and_quiet",
        "getting_smaller",
        "healthy_one_fund",
        "waiting_on_a_call",
        "more_urgent_but_not_called",
    }


def test_replay_compares_two_dates() -> None:
    response = client.get(f"{BASE}/replay", params={"date_a": "2026-08-18", "date_b": "2026-09-01"})

    assert response.status_code == 200
    body = response.json()
    assert body["date_a"] == "2026-08-18"
    assert len(body["rows"]) == 8


def test_situation_mapping_returns_the_seeded_rows() -> None:
    response = client.get(f"{BASE}/situation-mapping", params={"as_of": AS_OF})

    assert response.status_code == 200
    body = response.json()
    situations = {row["situation"] for row in body["rows"]}
    assert "more_urgent_but_not_called" in situations
    assert body["pending_version"] is None


def test_configuration_reports_the_priority_order() -> None:
    response = client.get(f"{BASE}/configuration", params={"as_of": AS_OF})

    assert response.status_code == 200
    body = response.json()
    assert body["situation_priority"][0] == "more_urgent_but_not_called"


def test_angle_brief_for_a_real_seeded_angle() -> None:
    response = client.get(f"{BASE}/angles/fee_warning", params={"as_of": AS_OF})

    assert response.status_code == 200
    body = response.json()
    assert body["angle"] == "fee_warning"
    assert body["headline"]


def test_angle_brief_for_an_unknown_angle_is_404() -> None:
    response = client.get(f"{BASE}/angles/not_a_real_angle", params={"as_of": AS_OF})

    assert response.status_code == 404


def test_situation_mapping_draft_is_refused_when_invalid() -> None:
    response = client.post(
        f"{BASE}/situation-mapping/draft",
        json={
            "rows": [
                {
                    "situation": "healthy_one_fund",
                    "action_code": "suggest_second_fund",
                    "objective": "not_a_real_objective",
                    "angle": "wrong_shelf",
                    "evidence_required": "a healthy risk band",
                    "channel": "email",
                }
            ]
        },
    )

    assert response.status_code == 422


def test_situation_mapping_draft_stage_and_discard() -> None:
    response = client.post(
        f"{BASE}/situation-mapping/draft",
        json={
            "rows": [
                {
                    "situation": "healthy_one_fund",
                    "action_code": "suggest_second_fund",
                    "objective": "encourage",
                    "angle": "wrong_shelf",
                    "evidence_required": "a healthy risk band and exactly one fund",
                    "channel": "email",
                }
            ]
        },
    )
    assert response.status_code == 200
    version = response.json()["version"]

    try:
        mapping = client.get(f"{BASE}/situation-mapping", params={"as_of": AS_OF})
        assert mapping.json()["pending_version"] == version
    finally:
        discard = client.post(f"{BASE}/situation-mapping/{version}/discard", json={})
        assert discard.status_code == 200

    with SessionLocal() as session:
        session.execute(
            delete(SituationActionMapping).where(SituationActionMapping.version == version)
        )
        session.commit()
