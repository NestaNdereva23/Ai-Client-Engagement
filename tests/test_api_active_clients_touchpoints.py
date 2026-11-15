from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.active_clients import (
    CONTACT_INTERACTION_TYPES,
    HANDLED_INTERACTION_TYPES,
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveClientTouchpoint,
)
from app.db.models.audit import AuditLog
from app.db.models.fa_assignment import FaAssignment
from app.db.models.routing import ClientTier
from app.db.models.user_access import ROLE_ADVISOR, UserAccess
from app.db.session import SessionLocal
from app.main import app
from app.routing.tiers import TIER_GRADUAL_DEPOSITOR
from app.services.client_touchpoints import today_in_nairobi

client = TestClient(app)

BASE = "/api/v1/active-clients"
OWNER = "touchpoint-owner"
STRANGER = "touchpoint-stranger"
FUND = 9811
CLIENT = 981101
OUTSIDE_BOOK = 981199


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, headers_for):
    client.headers.update(headers_for(OWNER))
    yield
    client.headers.pop("Authorization", None)


def _clear(session) -> None:
    session.execute(
        delete(ActiveClientTouchpoint).where(ActiveClientTouchpoint.client_id == CLIENT)
    )
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id == CLIENT)
    )
    session.execute(delete(AuditLog).where(AuditLog.entity_id == f"{CLIENT}/{FUND}"))
    for model in (FaAssignment, ActiveClientFund, ClientTier):
        session.execute(delete(model).where(model.client_id == CLIENT))
    session.execute(delete(UserAccess).where(UserAccess.email.in_([OWNER, STRANGER])))
    session.commit()


@pytest.fixture
def seeded_client(db):
    with SessionLocal() as session:
        _clear(session)
        session.add(
            ActiveClientFund(
                client_id=CLIENT,
                unit_fund_id=FUND,
                client_code=f"C{CLIENT}",
                balance=10_000.0,
                n_deposits=2,
                n_withdrawals=0,
            )
        )
        session.add(FaAssignment(client_id=CLIENT, unit_fund_id=FUND, fa_id=OWNER, source="test"))
        session.add_all(
            [
                UserAccess(email=OWNER, role=ROLE_ADVISOR, fa_id=OWNER),
                UserAccess(email=STRANGER, role=ROLE_ADVISOR, fa_id=STRANGER),
            ]
        )
        session.add(
            ClientTier(
                client_id=CLIENT,
                tier=TIER_GRADUAL_DEPOSITOR,
                segment="active_active",
                purchased_recently=True,
                real_withdrawer=False,
                computed_at=datetime.now(UTC),
                thresholds_version=1,
                run_id="test",
            )
        )
        session.commit()
    yield
    with SessionLocal() as session:
        _clear(session)


def _in_days(days: int) -> str:
    return (today_in_nairobi() + timedelta(days=days)).isoformat()


def _touchpoint_body(**changes) -> dict:
    body = {"kind": "call", "title": "Check-in call", "due_date": _in_days(3), "due_time": "10:30"}
    return {**body, **changes}


def _add(client_id: int = CLIENT, headers: dict | None = None, **changes):
    return client.post(
        f"{BASE}/{client_id}/{FUND}/touchpoints",
        headers=headers,
        json=_touchpoint_body(**changes),
    )


def _open_touchpoints() -> list[dict]:
    return client.get(f"{BASE}/{CLIENT}/{FUND}/touchpoints").json()


def _log(body: dict):
    return client.post(f"{BASE}/{CLIENT}/{FUND}/interactions", json=body)


def test_the_owner_can_add_list_and_remove_a_touchpoint(seeded_client) -> None:
    created = _add(minutes=30)

    assert created.status_code == 201
    touchpoint = created.json()
    assert touchpoint["created_by"] == OWNER
    assert touchpoint["due_time"] == "10:30:00"
    assert [row["id"] for row in _open_touchpoints()] == [touchpoint["id"]]

    removed = client.delete(f"{BASE}/{CLIENT}/{FUND}/touchpoints/{touchpoint['id']}")

    assert removed.status_code == 204
    assert _open_touchpoints() == []
    with SessionLocal() as session:
        actions = session.scalars(
            select(AuditLog.action)
            .where(
                AuditLog.entity_type == "active_client_touchpoint",
                AuditLog.entity_id == f"{CLIENT}/{FUND}",
            )
            .order_by(AuditLog.log_id)
        ).all()
    assert actions == ["create", "remove"]


def test_only_the_owner_can_change_touchpoints_and_the_client_must_be_in_the_book(
    seeded_client, headers_for, reviewer_1_headers
) -> None:
    stranger = headers_for(STRANGER)

    assert _add(headers=stranger).status_code == 403
    assert _add(client_id=OUTSIDE_BOOK).status_code == 403
    assert _add(client_id=OUTSIDE_BOOK, headers=reviewer_1_headers).status_code == 404
    assert _open_touchpoints() == []

    touchpoint_id = _add().json()["id"]
    stranger_remove = client.delete(
        f"{BASE}/{CLIENT}/{FUND}/touchpoints/{touchpoint_id}", headers=stranger
    )

    assert stranger_remove.status_code == 403
    assert len(_open_touchpoints()) == 1


def test_a_touchpoint_cannot_be_in_the_past_or_carry_minutes_on_an_email(seeded_client) -> None:
    assert _add(due_date=_in_days(-1)).status_code == 422
    assert _add(kind="email", minutes=30).status_code == 422


def test_a_snooze_with_a_day_adds_a_reminder_on_that_day(seeded_client) -> None:
    response = _log({"type": "snoozed", "follow_up_on": _in_days(7)})

    assert response.status_code == 201
    [reminder] = _open_touchpoints()
    assert reminder["kind"] == "reminder"
    assert reminder["due_date"] == _in_days(7)


def test_a_follow_up_outcome_adds_a_touchpoint_and_keeps_channel_and_outcome(
    seeded_client,
) -> None:
    response = _log({"type": "meeting_logged", "outcome": "follow_up", "follow_up_on": _in_days(5)})

    assert response.status_code == 201
    assert response.json()["channel"] == "meeting"
    assert response.json()["outcome"] == "follow_up"
    [follow_up] = _open_touchpoints()
    assert follow_up["kind"] == "meeting"
    assert follow_up["due_date"] == _in_days(5)


def test_logging_a_call_against_a_scheduled_touchpoint_marks_it_done(seeded_client) -> None:
    touchpoint_id = _add().json()["id"]

    response = _log({"type": "call_logged", "outcome": "connected", "touchpoint_id": touchpoint_id})

    assert response.status_code == 201
    assert _open_touchpoints() == []
    with SessionLocal() as session:
        done = session.get(ActiveClientTouchpoint, touchpoint_id)
        assert done.status == "done"
        assert done.done_by_interaction_id == response.json()["id"]
    assert _log({"type": "call_logged", "touchpoint_id": touchpoint_id}).status_code == 404


def test_a_follow_up_day_in_the_past_is_refused_and_nothing_is_logged(seeded_client) -> None:
    response = _log({"type": "snoozed", "follow_up_on": _in_days(-2)})

    assert response.status_code == 422
    assert client.get(f"{BASE}/{CLIENT}/{FUND}/interactions").json() == []


def test_a_day_without_a_snooze_or_follow_up_is_refused(seeded_client) -> None:
    response = _log({"type": "call_logged", "outcome": "connected", "follow_up_on": _in_days(2)})

    assert response.status_code == 422


@pytest.mark.parametrize("new_type", ["meeting_logged", "whatsapp_logged"])
def test_meeting_and_whatsapp_count_as_contact_and_as_handled(new_type) -> None:
    assert new_type in CONTACT_INTERACTION_TYPES
    assert new_type in HANDLED_INTERACTION_TYPES


def test_the_profile_carries_the_short_fund_name_success_measure_and_review_counts(
    seeded_client,
) -> None:
    profile = client.get(f"{BASE}/{CLIENT}/{FUND}/profile").json()

    assert profile["identity"]["fund_short_name"]
    assert profile["segment_and_tier"]["success_measure"]
    assert profile["bands"]["deposit_trend_band"] == "unknown"
    assert profile["review_activity"]["review_period_days"] > 0
    assert profile["review_activity"]["deposits"] == 0
    assert profile["review_activity"]["withdrawals"] == 0
