"""The business-rules console API: browse versions, and dry-run a preview.

Uses a high, otherwise-unused version number, matching the convention in
test_rules_store.py, so this never collides with the real seeded rule sets.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.rules import BusinessRule, MessageAngleCatalog
from app.db.session import SessionLocal
from app.main import app
from app.rules.catalog import AngleSpec, save_catalog_version
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
                    name="capped",
                    priority=10,
                    match={"purchase_depth": ["capped"]},
                    message_angle="back_on_schedule",
                    urgency="high",
                    priority_tier="T1",
                    prompt_variant="back_on_schedule",
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
        json={"purchase_depth": "capped", "at": _PREVIEW_AT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message_angle"] == "back_on_schedule"
    assert body["rule_name"] == "capped"
    assert body["version"] == active_version


def test_preview_falls_through_to_the_catch_all(active_version) -> None:
    response = client.post(f"{RULES}/preview", json={"purchase_depth": "single", "at": _PREVIEW_AT})
    assert response.status_code == 200
    assert response.json()["rule_name"] == "catch_all"


def test_preview_422s_when_no_rule_set_is_active(active_version) -> None:
    response = client.post(
        f"{RULES}/preview", json={"purchase_depth": "single", "at": "1900-01-01"}
    )
    assert response.status_code == 422


# --- GET /rules/angles: current held state ----------------------------------


def _angle_spec(angle: str, *, held: bool) -> AngleSpec:
    return AngleSpec(
        angle=angle,
        headline="test headline",
        who="test who",
        claim="test claim",
        ask="test ask",
        never="test never",
        held=held,
    )


@pytest.fixture
def held_and_unheld_angles(db: None):
    held_angle = "test_held_angle_91"
    unheld_angle = "test_unheld_angle_91"
    with SessionLocal() as session:
        save_catalog_version(
            session,
            _TEST_VERSION,
            [_angle_spec(held_angle, held=True), _angle_spec(unheld_angle, held=False)],
            valid_from=date(2020, 1, 1),
        )
        session.commit()

    yield held_angle, unheld_angle

    with SessionLocal() as session:
        session.execute(
            delete(MessageAngleCatalog).where(MessageAngleCatalog.version == _TEST_VERSION)
        )
        session.commit()


def test_get_angles_reports_held_state(held_and_unheld_angles) -> None:
    held_angle, unheld_angle = held_and_unheld_angles
    response = client.get(f"{RULES}/angles")
    assert response.status_code == 200
    rows = {row["angle"]: row["held"] for row in response.json()}
    assert rows[held_angle] is True
    assert rows[unheld_angle] is False


def test_get_angles_defaults_to_today_not_a_future_window(held_and_unheld_angles) -> None:
    held_angle, _unheld_angle = held_and_unheld_angles
    response = client.get(f"{RULES}/angles", params={"active_on": "1900-01-01"})
    assert response.status_code == 200
    angles = {row["angle"] for row in response.json()}
    assert held_angle not in angles
