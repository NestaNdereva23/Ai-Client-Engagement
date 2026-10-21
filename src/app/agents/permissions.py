"""Read and change how much freedom each action has.

A setting may be written for an action alone, or narrowed to a priority tier,
a risk band, or both. A lookup takes the most specific row that matches, so a
general rule can be tightened for one group without repeating it everywhere.

Every change goes through set_permission, which writes an audit row holding
the old value, the new value and the reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agents.action_rules import ALWAYS_NEEDS_A_PERSON
from app.audit.log import record_audit
from app.config import get_settings
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
    at: date | None = None,
) -> AgentPermission | None:
    """The row that governs this action for these clients, or None if there is none."""
    at = at or date.today()
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
                AgentPermission.valid_from <= at,
                or_(AgentPermission.valid_to.is_(None), AgentPermission.valid_to > at),
            )
        )
        if row is not None:
            return row
    return None


APPROVE_EACH = "approve_each"


def effective_permission(
    session: Session,
    action_code: str,
    *,
    priority_tier: str | None = None,
    risk_band: str | None = None,
    money_total_kes: float | None = None,
    money_ceiling_kes: float | None = None,
    at: date | None = None,
) -> str:
    """The level in force, falling back to the safest one when nothing is set."""
    if get_settings().agent_force_approve_each:
        return APPROVE_EACH
    if (
        money_ceiling_kes is not None
        and money_total_kes is not None
        and money_total_kes > money_ceiling_kes
    ):
        return APPROVE_EACH
    row = resolve_permission(
        session, action_code, priority_tier=priority_tier, risk_band=risk_band, at=at
    )
    level = DEFAULT_PERMISSION if row is None else row.permission
    if action_code in ALWAYS_NEEDS_A_PERSON:
        return _at_most_approve_each(level)
    return level


def _at_most_approve_each(level: str) -> str:
    if PERMISSION_LEVELS.index(level) > PERMISSION_LEVELS.index(APPROVE_EACH):
        return APPROVE_EACH
    return level


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
    valid_from: date | None = None,
) -> AgentPermission:
    """Close the current setting and open a new one, and record what changed and why."""
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

    start = valid_from or date.today()
    row = session.scalar(
        select(AgentPermission).where(
            AgentPermission.action_code == action_code,
            AgentPermission.priority_tier == priority_tier,
            AgentPermission.risk_band == risk_band,
            AgentPermission.valid_to.is_(None),
        )
    )
    before = _snapshot(row)
    if (
        row is not None
        and row.permission == permission
        and row.max_clients_per_day == max_clients_per_day
        and row.max_money_kes == max_money_kes
    ):
        return row

    if row is not None:
        row.valid_to = start

    new_row = AgentPermission(
        action_code=action_code,
        priority_tier=priority_tier,
        risk_band=risk_band,
        permission=permission,
        max_clients_per_day=max_clients_per_day,
        max_money_kes=max_money_kes,
        changed_by=changed_by,
        changed_reason=changed_reason,
        valid_from=start,
    )
    session.add(new_row)
    session.flush()

    record_audit(
        session,
        entity_type=_ENTITY_TYPE,
        entity_id=str(new_row.permission_id),
        action="create" if before is None else "update",
        actor_id=changed_by,
        detail={
            "action_code": action_code,
            "priority_tier": priority_tier,
            "risk_band": risk_band,
            "before": before,
            "after": _snapshot(new_row),
            "reason": changed_reason,
        },
    )
    return new_row


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
                AgentPermission.valid_to.is_(None),
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
