"""Read and change how much freedom each action has.

A setting may be written for an action alone, or narrowed to a priority tier,
a risk band, or both. A lookup takes the most specific row that matches, so a
general rule can be tightened for one group without repeating it everywhere.

Every change goes through set_permission, which writes an audit row holding
the old value, the new value and the reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.agent import PERMISSION_LEVELS
from app.db.models.agent_permission import DEFAULT_PERMISSION, AgentPermission

_ENTITY_TYPE = "agent_permission"

# Most specific first: both, then tier only, then band only, then the action alone.
_NARROWING_ORDER = ((True, True), (True, False), (False, True), (False, False))


class PermissionValidationError(ValueError):
    """A setting was refused and nothing was written."""


def resolve_permission(
    session: Session,
    action_code: str,
    *,
    priority_tier: str | None = None,
    risk_band: str | None = None,
) -> AgentPermission | None:
    """The row that governs this action for these clients, or None if there is none."""
    for use_tier, use_band in _NARROWING_ORDER:
        if use_tier and priority_tier is None:
            continue
        if use_band and risk_band is None:
            continue
        row = session.scalar(
            select(AgentPermission).where(
                AgentPermission.action_code == action_code,
                AgentPermission.priority_tier == (priority_tier if use_tier else None),
                AgentPermission.risk_band == (risk_band if use_band else None),
            )
        )
        if row is not None:
            return row
    return None


def effective_permission(
    session: Session,
    action_code: str,
    *,
    priority_tier: str | None = None,
    risk_band: str | None = None,
) -> str:
    """The level in force, falling back to the safest one when nothing is set."""
    row = resolve_permission(session, action_code, priority_tier=priority_tier, risk_band=risk_band)
    return DEFAULT_PERMISSION if row is None else row.permission


def _snapshot(row: AgentPermission | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "permission": row.permission,
        "max_clients_per_day": row.max_clients_per_day,
        "max_money_kes": row.max_money_kes,
    }


def set_permission(
    session: Session,
    action_code: str,
    permission: str,
    *,
    changed_by: str,
    changed_reason: str,
    priority_tier: str | None = None,
    risk_band: str | None = None,
    max_clients_per_day: int | None = None,
    max_money_kes: float | None = None,
) -> AgentPermission:
    """Write or update one setting and record what changed and why."""
    if permission not in PERMISSION_LEVELS:
        raise PermissionValidationError(f"unknown permission level '{permission}'")
    if not str(changed_by).strip():
        raise PermissionValidationError("a change needs to say who made it")
    if not str(changed_reason).strip():
        raise PermissionValidationError("a change needs a reason")
    if max_clients_per_day is not None and max_clients_per_day <= 0:
        raise PermissionValidationError("a daily client cap must be above zero")
    if max_money_kes is not None and max_money_kes <= 0:
        raise PermissionValidationError("a daily money cap must be above zero")

    row = session.scalar(
        select(AgentPermission).where(
            AgentPermission.action_code == action_code,
            AgentPermission.priority_tier == priority_tier,
            AgentPermission.risk_band == risk_band,
        )
    )
    before = _snapshot(row)

    if row is None:
        row = AgentPermission(
            action_code=action_code,
            priority_tier=priority_tier,
            risk_band=risk_band,
        )
        session.add(row)

    row.permission = permission
    row.max_clients_per_day = max_clients_per_day
    row.max_money_kes = max_money_kes
    row.changed_by = changed_by
    row.changed_reason = changed_reason
    session.flush()

    record_audit(
        session,
        entity_type=_ENTITY_TYPE,
        entity_id=str(row.permission_id),
        action="create" if before is None else "update",
        actor_id=changed_by,
        detail={
            "action_code": action_code,
            "priority_tier": priority_tier,
            "risk_band": risk_band,
            "before": before,
            "after": _snapshot(row),
            "reason": changed_reason,
        },
    )
    return row


def seed_default_permissions(
    session: Session,
    action_codes: Sequence[str],
    *,
    changed_by: str,
    changed_reason: str,
) -> int:
    """Give every action the safest level, skipping any that already has a setting."""
    written = 0
    for action_code in action_codes:
        already = session.scalar(
            select(AgentPermission).where(
                AgentPermission.action_code == action_code,
                AgentPermission.priority_tier.is_(None),
                AgentPermission.risk_band.is_(None),
            )
        )
        if already is not None:
            continue
        set_permission(
            session,
            action_code,
            DEFAULT_PERMISSION,
            changed_by=changed_by,
            changed_reason=changed_reason,
        )
        written += 1
    return written
