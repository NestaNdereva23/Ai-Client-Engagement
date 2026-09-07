"""Agent permissions: the seeded default, the narrowing order, and the audit trail."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents.permissions import (
    PermissionValidationError,
    effective_permission,
    resolve_permission,
    seed_default_permissions,
    set_permission,
)
from app.db.models.agent_permission import DEFAULT_PERMISSION, AgentPermission
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal

SEEDED_ACTIONS = (
    "welcome_and_top_up",
    "fee_warning",
    "start_win_back",
    "ask_what_changed",
    "suggest_second_fund",
    "send_learning_note",
    "follow_up_when_no_one_called",
    "do_nothing",
)

TRIAL_ACTION = "trial_permission_action"


@pytest.fixture
def written_actions():
    """Remove the rows a test writes, so the seeded settings are all that is left."""
    codes: list[str] = []
    yield codes
    if not codes:
        return
    with SessionLocal() as session:
        session.execute(delete(AgentPermission).where(AgentPermission.action_code.in_(codes)))
        session.execute(delete(AuditLog).where(AuditLog.entity_type == "agent_permission"))
        session.commit()


def test_every_seeded_action_has_a_setting(db: None) -> None:
    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentPermission).where(AgentPermission.priority_tier.is_(None))
        ).all()
    settings = {row.action_code: row.permission for row in rows}
    for action_code in SEEDED_ACTIONS:
        assert settings[action_code] == "suggest_only"


def test_a_seeded_setting_carries_no_caps(db: None) -> None:
    with SessionLocal() as session:
        row = resolve_permission(session, "start_win_back")
    assert row is not None
    assert row.max_clients_per_day is None
    assert row.max_money_kes is None


def test_an_action_with_no_setting_falls_back_to_the_safest_level(db: None) -> None:
    with SessionLocal() as session:
        assert resolve_permission(session, "never_configured") is None
        assert effective_permission(session, "never_configured") == DEFAULT_PERMISSION


def test_the_action_wide_setting_is_used_when_nothing_narrower_exists(
    db: None, written_actions: list[str]
) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session,
            TRIAL_ACTION,
            "approve_each",
            changed_by="tester",
            changed_reason="starting point",
        )
        session.commit()

    with SessionLocal() as session:
        assert (
            effective_permission(session, TRIAL_ACTION, priority_tier="T1", risk_band="high")
            == "approve_each"
        )


def test_a_tier_row_beats_the_action_wide_row(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session, TRIAL_ACTION, "approve_each", changed_by="tester", changed_reason="wide"
        )
        set_permission(
            session,
            TRIAL_ACTION,
            "approve_sample",
            priority_tier="T1",
            changed_by="tester",
            changed_reason="tier one is safe enough",
        )
        session.commit()

    with SessionLocal() as session:
        assert effective_permission(session, TRIAL_ACTION, priority_tier="T1") == "approve_sample"
        assert effective_permission(session, TRIAL_ACTION, priority_tier="T4") == "approve_each"


def test_a_band_row_beats_the_action_wide_row(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session, TRIAL_ACTION, "act_alone", changed_by="tester", changed_reason="wide"
        )
        set_permission(
            session,
            TRIAL_ACTION,
            "suggest_only",
            risk_band="high",
            changed_by="tester",
            changed_reason="high risk needs a person",
        )
        session.commit()

    with SessionLocal() as session:
        assert effective_permission(session, TRIAL_ACTION, risk_band="high") == "suggest_only"
        assert effective_permission(session, TRIAL_ACTION, risk_band="low") == "act_alone"


def test_a_tier_and_band_row_beats_a_tier_only_row(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session,
            TRIAL_ACTION,
            "act_alone",
            priority_tier="T1",
            changed_by="tester",
            changed_reason="tier only",
        )
        set_permission(
            session,
            TRIAL_ACTION,
            "approve_each",
            priority_tier="T1",
            risk_band="high",
            changed_by="tester",
            changed_reason="both",
        )
        session.commit()

    with SessionLocal() as session:
        assert (
            effective_permission(session, TRIAL_ACTION, priority_tier="T1", risk_band="high")
            == "approve_each"
        )
        assert (
            effective_permission(session, TRIAL_ACTION, priority_tier="T1", risk_band="low")
            == "act_alone"
        )


def test_a_tier_only_row_beats_a_band_only_row(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session,
            TRIAL_ACTION,
            "act_alone",
            priority_tier="T1",
            changed_by="tester",
            changed_reason="tier",
        )
        set_permission(
            session,
            TRIAL_ACTION,
            "suggest_only",
            risk_band="high",
            changed_by="tester",
            changed_reason="band",
        )
        session.commit()

    with SessionLocal() as session:
        assert (
            effective_permission(session, TRIAL_ACTION, priority_tier="T1", risk_band="high")
            == "act_alone"
        )


def test_writing_a_setting_twice_updates_the_same_row(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        first = set_permission(
            session, TRIAL_ACTION, "suggest_only", changed_by="tester", changed_reason="first"
        )
        first_id = first.permission_id
        second = set_permission(
            session,
            TRIAL_ACTION,
            "approve_each",
            max_clients_per_day=50,
            max_money_kes=250000.0,
            changed_by="tester",
            changed_reason="second",
        )
        assert second.permission_id == first_id
        session.commit()

    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentPermission).where(AgentPermission.action_code == TRIAL_ACTION)
        ).all()
    assert len(rows) == 1
    assert rows[0].permission == "approve_each"
    assert rows[0].max_clients_per_day == 50
    assert rows[0].max_money_kes == 250000.0


def test_a_change_leaves_a_readable_trail(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session, TRIAL_ACTION, "suggest_only", changed_by="asha", changed_reason="starting out"
        )
        set_permission(
            session,
            TRIAL_ACTION,
            "approve_sample",
            changed_by="asha",
            changed_reason="two clean weeks",
        )
        session.commit()

    with SessionLocal() as session:
        rows = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "agent_permission")
            .order_by(AuditLog.log_id)
        ).all()
    changes = [row for row in rows if row.detail["action_code"] == TRIAL_ACTION]
    assert [row.action for row in changes] == ["create", "update"]
    assert changes[0].detail["before"] is None
    assert changes[1].detail["before"]["permission"] == "suggest_only"
    assert changes[1].detail["after"]["permission"] == "approve_sample"
    assert changes[1].detail["reason"] == "two clean weeks"
    assert changes[1].actor_id == "asha"


def test_an_unknown_permission_level_is_refused(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(PermissionValidationError, match="unknown permission level"):
            set_permission(
                session,
                TRIAL_ACTION,
                "whenever_it_likes",
                changed_by="tester",
                changed_reason="no",
            )
        session.rollback()


@pytest.mark.parametrize("field", ["changed_by", "changed_reason"])
def test_a_change_must_say_who_and_why(db: None, field: str) -> None:
    fields = {"changed_by": "tester", "changed_reason": "because"}
    fields[field] = "   "
    with SessionLocal() as session:
        with pytest.raises(PermissionValidationError):
            set_permission(session, TRIAL_ACTION, "suggest_only", **fields)
        session.rollback()


def test_a_cap_must_be_above_zero(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(PermissionValidationError, match="client cap"):
            set_permission(
                session,
                TRIAL_ACTION,
                "suggest_only",
                max_clients_per_day=0,
                changed_by="tester",
                changed_reason="no",
            )
        with pytest.raises(PermissionValidationError, match="money cap"):
            set_permission(
                session,
                TRIAL_ACTION,
                "suggest_only",
                max_money_kes=0.0,
                changed_by="tester",
                changed_reason="no",
            )
        session.rollback()


def test_seeding_leaves_an_existing_setting_alone(db: None, written_actions: list[str]) -> None:
    with SessionLocal() as session:
        written_actions.append(TRIAL_ACTION)
        set_permission(
            session, TRIAL_ACTION, "act_alone", changed_by="tester", changed_reason="earned it"
        )
        written = seed_default_permissions(
            session,
            [TRIAL_ACTION, "another_new_action"],
            changed_by="system",
            changed_reason="starting setting",
        )
        written_actions.append("another_new_action")
        session.commit()

    assert written == 1
    with SessionLocal() as session:
        assert effective_permission(session, TRIAL_ACTION) == "act_alone"
        assert effective_permission(session, "another_new_action") == DEFAULT_PERMISSION
