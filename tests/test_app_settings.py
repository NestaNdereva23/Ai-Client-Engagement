from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models.app_setting import AppSetting
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)
URL = "/api/v1/app-settings"


@pytest.fixture
def clean_settings(db: None, configured_reviewers: None):
    yield
    with SessionLocal() as session:
        session.execute(delete(AppSetting))
        session.commit()
    get_settings.reload_overrides()


def _save(headers: dict[str, str], changes: dict, **extra):
    body = {"changes": changes, "reason": "Testing the settings page", **extra}
    return client.put(URL, json=body, headers=headers)


def _value(response, key: str):
    return next(s["value"] for s in response.json()["settings"] if s["key"] == key)


def test_saved_setting_takes_effect_at_once_and_is_audited(
    clean_settings: None, reviewer_1_headers: dict[str, str]
) -> None:
    before = get_settings().test_campaign_max_clients
    wanted = 7 if before != 7 else 8

    response = _save(reviewer_1_headers, {"test_campaign_max_clients": wanted})

    assert response.status_code == 200
    assert _value(response, "test_campaign_max_clients") == wanted
    assert get_settings().test_campaign_max_clients == wanted
    with SessionLocal() as session:
        audit = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "app_setting")
            .where(AuditLog.entity_id == "test_campaign_max_clients")
            .order_by(AuditLog.log_id.desc())
        ).first()
    assert audit.actor_id == "fa-1"
    assert audit.detail == {"from": before, "to": wanted, "reason": "Testing the settings page"}


@pytest.mark.parametrize(
    "changes",
    [
        {"agent_query_min_group_size": 2},
        {"rag_min_score": 1.5},
        {"delivery_mode": "sometimes"},
        {"database_url": "postgresql://elsewhere"},
    ],
)
def test_invalid_change_is_refused_and_nothing_is_saved(
    clean_settings: None, reviewer_1_headers: dict[str, str], changes: dict
) -> None:
    response = _save(reviewer_1_headers, {"test_campaign_max_clients": 9, **changes})

    assert response.status_code == 422
    with SessionLocal() as session:
        assert session.scalars(select(AppSetting)).all() == []


def test_switching_to_live_needs_confirmation(
    clean_settings: None, reviewer_1_headers: dict[str, str]
) -> None:
    assert _save(reviewer_1_headers, {"delivery_mode": "test"}).status_code == 200

    refused = _save(reviewer_1_headers, {"delivery_mode": "live"})
    confirmed = _save(reviewer_1_headers, {"delivery_mode": "live"}, confirm_live=True)

    assert refused.status_code == 422
    assert confirmed.status_code == 200
    assert get_settings().delivery_mode == "live"


def test_turning_off_the_live_sms_redirect_needs_confirmation(
    clean_settings: None, reviewer_1_headers: dict[str, str]
) -> None:
    assert _save(reviewer_1_headers, {"live_sms_to_test_list": True}).status_code == 200

    refused = _save(reviewer_1_headers, {"live_sms_to_test_list": False})
    confirmed = _save(reviewer_1_headers, {"live_sms_to_test_list": False}, confirm_live=True)

    assert refused.status_code == 422
    assert confirmed.status_code == 200
    assert get_settings().live_sms_to_test_list is False
