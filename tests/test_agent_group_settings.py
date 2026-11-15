from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents.fa_scope import fa_scope
from app.agents.permissions import (
    GROUP_PAUSED,
    OUTSIDE_BALANCE_RANGE,
    OVER_GROUP_LIMIT,
    PermissionValidationError,
    apply_group_rules,
    effective_permission,
    load_group_settings,
    save_group_setting,
)
from app.db.models.agent_permission import AgentPermission
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal

GROUP = "trial_group_settings"
ADVISER = "trial_adviser"


@pytest.fixture
def session(db: None):
    with SessionLocal() as session:
        yield session
        session.rollback()
        session.execute(delete(AgentPermission).where(AgentPermission.group_name == GROUP))
        session.execute(delete(AuditLog).where(AuditLog.entity_type == "agent_permission"))
        session.commit()


def _member(client_id: int, balance: float, fund: int = 1) -> SimpleNamespace:
    return SimpleNamespace(client_id=client_id, unit_fund_id=fund, balance=balance)


def test_adviser_row_overrides_only_what_it_sets(session) -> None:
    save_group_setting(
        session,
        GROUP,
        {"permission": "approve_each", "balance_filter": True, "min_balance": 6000},
        fa_id=None,
        changed_by="tester",
        changed_reason="start",
    )
    save_group_setting(
        session,
        GROUP,
        {"max_clients_per_day": 20},
        fa_id=ADVISER,
        changed_by="tester",
        changed_reason=None,
    )

    own = load_group_settings(session, GROUP, ADVISER)
    assert own.permission == "approve_each"
    assert own.min_balance == 6000
    assert own.max_clients_per_day == 20
    assert load_group_settings(session, GROUP, None).max_clients_per_day == 0


def test_group_level_wins_over_the_action_row(session) -> None:
    save_group_setting(
        session,
        GROUP,
        {"permission": "act_alone"},
        fa_id=None,
        changed_by="tester",
        changed_reason="trusted",
    )
    assert effective_permission(session, "ask_what_changed", group_name=GROUP) == "act_alone"


def test_a_freedom_change_needs_a_reason(session) -> None:
    with pytest.raises(PermissionValidationError):
        save_group_setting(
            session,
            GROUP,
            {"permission": "act_alone"},
            fa_id=None,
            changed_by="tester",
            changed_reason=" ",
        )


def test_balance_range_and_limit_leave_the_right_clients_out(session) -> None:
    save_group_setting(
        session,
        GROUP,
        {"balance_filter": True, "min_balance": 6000, "max_clients_per_day": 2},
        fa_id=None,
        changed_by="tester",
        changed_reason=None,
    )
    # Client 3 holds two funds, so it counts once against the limit.
    members = [
        _member(1, 500),
        _member(2, 9000),
        _member(3, 8000),
        _member(3, 7000, fund=2),
        _member(4, 12000),
    ]
    reasons = {(m.client_id, m.unit_fund_id): None for m in members}

    out = apply_group_rules(session, GROUP, members, reasons)

    assert out[(1, 1)] == OUTSIDE_BALANCE_RANGE
    assert sorted(v for v in out.values() if v == OVER_GROUP_LIMIT) == [OVER_GROUP_LIMIT]
    kept_clients = {key[0] for key, value in out.items() if value is None}
    assert len(kept_clients) == 2


def test_the_limit_is_per_adviser(session) -> None:
    save_group_setting(
        session,
        GROUP,
        {"max_clients_per_day": 1},
        fa_id=ADVISER,
        changed_by="tester",
        changed_reason=None,
    )
    members = [_member(1, 9000), _member(2, 9000)]
    reasons = {(m.client_id, m.unit_fund_id): None for m in members}

    with fa_scope(ADVISER):
        limited = apply_group_rules(session, GROUP, members, reasons)
    everyone = apply_group_rules(session, GROUP, members, reasons)

    assert list(limited.values()).count(OVER_GROUP_LIMIT) == 1
    assert OVER_GROUP_LIMIT not in everyone.values()


def test_a_paused_group_keeps_earlier_reasons(session) -> None:
    save_group_setting(
        session, GROUP, {"paused": True}, fa_id=None, changed_by="tester", changed_reason=None
    )
    members = [_member(1, 9000), _member(2, 9000)]
    reasons = {(1, 1): "open_complaint", (2, 1): None}

    out = apply_group_rules(session, GROUP, members, reasons)

    assert out == {(1, 1): "open_complaint", (2, 1): GROUP_PAUSED}
