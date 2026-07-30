"""The business-rules console API: browse versions, and dry-run a preview.

Uses a high, otherwise-unused version number, matching the convention in
test_rules_store.py, so this never collides with the real seeded rule sets.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.rules import BusinessRule
from app.db.session import SessionLocal
from app.main import app
from app.rules.store import RuleSpec, save_version

client = TestClient(app)

RULES = "/api/v1/rules"

_TEST_VERSION = 91
# Far enough out that no real seeded version can ever have a later valid_from
# and win load_active_rules' tie-break instead of this one.
_FAR_FUTURE = date(2099, 1, 1)
_PREVIEW_AT = "2099-06-01"


@pytest.fixture
def active_version(db: None):
    with SessionLocal() as session:
        save_version(
            session,
            _TEST_VERSION,
            [
                RuleSpec(
                    name="frequent",
                    priority=10,
                    match={"archetype": ["Frequent (5+, censored)"]},
                    message_angle="winback_habit",
                    urgency="high",
                    priority_tier="P1",
                    prompt_variant="habit_premium",
                ),
                RuleSpec(name="catch_all", priority=20),
            ],
            valid_from=_FAR_FUTURE,
        )
        session.commit()

    yield _TEST_VERSION

    with SessionLocal() as session:
        session.execute(delete(BusinessRule).where(BusinessRule.version == _TEST_VERSION))
        session.commit()


def test_get_rule_versions_reports_the_window_and_rule_count(active_version) -> None:
    response = client.get(RULES, params={"version": active_version})
    assert response.status_code == 200
    (row,) = response.json()
    assert row["version"] == active_version
    assert row["rule_count"] == 2
    assert row["is_active"] is False  # active far in the future, not today


def test_preview_resolves_the_matching_rule(active_version) -> None:
    response = client.post(
        f"{RULES}/preview",
        json={"archetype": "Frequent (5+, censored)", "at": _PREVIEW_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message_angle"] == "winback_habit"
    assert body["rule_name"] == "frequent"
    assert body["version"] == active_version


def test_preview_falls_through_to_the_catch_all(active_version) -> None:
    response = client.post(
        f"{RULES}/preview", json={"archetype": "One-and-done", "at": _PREVIEW_AT}
    )
    assert response.status_code == 200
    assert response.json()["rule_name"] == "catch_all"


def test_preview_422s_when_no_rule_set_is_active(active_version) -> None:
    response = client.post(
        f"{RULES}/preview", json={"archetype": "One-and-done", "at": "1900-01-01"}
    )
    assert response.status_code == 422
