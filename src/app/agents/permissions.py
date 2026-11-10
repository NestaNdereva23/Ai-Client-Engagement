"""Read and change how much freedom each action has.

A setting may be written for an action alone, or narrowed to a priority tier,
a risk band, or both. A lookup takes the most specific row that matches, so a
general rule can be tightened for one group without repeating it everywhere.

Every change goes through set_permission, which writes an audit row holding
the old value, the new value and the reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agents.action_rules import ALWAYS_NEEDS_A_PERSON
from app.agents.fa_scope import current_fa_id
from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.agent import PERMISSION_LEVELS
from app.db.models.agent_permission import DEFAULT_PERMISSION, AgentPermission
from app.db.models.campaigns import Enrollment
from app.db.models.risk import ClientRiskFeatures

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
    group_name: str | None = None,
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
    group_level = None
    if group_name is not None:
        group_level = load_group_settings(session, group_name, current_fa_id(), at).permission
    row = resolve_permission(
        session, action_code, priority_tier=priority_tier, risk_band=risk_band, at=at
    )
    level = group_level or (DEFAULT_PERMISSION if row is None else row.permission)
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


OUTSIDE_BALANCE_RANGE = "outside_balance_range"
OVER_GROUP_LIMIT = "over_group_limit"
GROUP_PAUSED = "group_paused"

GROUP_FIELDS = (
    "permission",
    "balance_filter",
    "min_balance",
    "max_balance",
    "max_clients_per_day",
    "content_mix",
    "guide_doc_ids",
    "use_rates",
    "no_repeat",
    "paused",
)
# These three belong together, so an adviser row that sets one replaces all three.
BALANCE_FIELDS = ("balance_filter", "min_balance", "max_balance")
CONTENT_MIXES = ("learning_only", "mostly_learning", "balanced", "mostly_ask")


@dataclass(frozen=True)
class GroupSettings:
    permission: str | None = None
    balance_filter: bool = False
    min_balance: float | None = None
    max_balance: float | None = None
    max_clients_per_day: int = 0
    content_mix: str | None = None
    guide_doc_ids: tuple[int, ...] | None = None
    use_rates: bool = False
    no_repeat: bool = False
    paused: bool = False

    def balance_allows(self, balance: float) -> bool:
        if not self.balance_filter:
            return True
        if self.min_balance is not None and balance < self.min_balance:
            return False
        return self.max_balance is None or balance <= self.max_balance


def group_row(
    session: Session, group_name: str, fa_id: str | None, at: date | None = None
) -> AgentPermission | None:
    at = at or date.today()
    return session.scalar(
        select(AgentPermission)
        .where(
            AgentPermission.group_name == group_name,
            AgentPermission.fa_id.is_(None) if fa_id is None else AgentPermission.fa_id == fa_id,
            AgentPermission.valid_from <= at,
            or_(AgentPermission.valid_to.is_(None), AgentPermission.valid_to > at),
        )
        .order_by(AgentPermission.valid_from.desc(), AgentPermission.permission_id.desc())
        .limit(1)
    )


def load_group_settings(
    session: Session, group_name: str, fa_id: str | None, at: date | None = None
) -> GroupSettings:
    rows = [group_row(session, group_name, None, at)]
    if fa_id is not None:
        rows.append(group_row(session, group_name, fa_id, at))
    values: dict[str, Any] = {}
    for row in rows:
        if row is None:
            continue
        for field in GROUP_FIELDS:
            if field in BALANCE_FIELDS:
                continue
            value = getattr(row, field)
            if value is not None:
                values[field] = value
        if row.balance_filter is not None:
            values.update({field: getattr(row, field) for field in BALANCE_FIELDS})
    if values.get("guide_doc_ids") is not None:
        values["guide_doc_ids"] = tuple(values["guide_doc_ids"])
    return GroupSettings(**values)


def _group_snapshot(row: AgentPermission | None) -> dict[str, Any] | None:
    return None if row is None else {field: getattr(row, field) for field in GROUP_FIELDS}


def _validate_group_values(values: Mapping[str, Any]) -> None:
    unknown = set(values) - set(GROUP_FIELDS)
    if unknown:
        raise PermissionValidationError(f"unknown group settings: {sorted(unknown)}")
    permission = values.get("permission")
    if permission is not None and permission not in PERMISSION_LEVELS:
        raise PermissionValidationError(f"unknown permission level '{permission}'")
    mix = values.get("content_mix")
    if mix is not None and mix not in CONTENT_MIXES:
        raise PermissionValidationError(f"unknown guide mix '{mix}'")
    low, high = values.get("min_balance"), values.get("max_balance")
    if low is not None and low < 0:
        raise PermissionValidationError("the lowest balance cannot be below zero")
    if low is not None and high is not None and high <= low:
        raise PermissionValidationError("the highest balance must be above the lowest")
    limit = values.get("max_clients_per_day")
    if limit is not None and limit < 0:
        raise PermissionValidationError("a client limit cannot be below zero")


def save_group_setting(
    session: Session,
    group_name: str,
    values: Mapping[str, Any],
    *,
    fa_id: str | None,
    changed_by: str,
    changed_reason: str | None,
    valid_from: date | None = None,
) -> AgentPermission | None:
    """Replace the live row for this group and adviser. A missing value follows the wider row."""
    _validate_group_values(values)
    if not str(changed_by).strip():
        raise PermissionValidationError("a change needs to say who made it")

    start = valid_from or date.today()
    current = session.scalar(
        select(AgentPermission).where(
            AgentPermission.group_name == group_name,
            AgentPermission.fa_id.is_(None) if fa_id is None else AgentPermission.fa_id == fa_id,
            AgentPermission.valid_to.is_(None),
        )
    )
    before = _group_snapshot(current)
    after = {field: values.get(field) for field in GROUP_FIELDS}
    if before == after:
        return current
    level_changed = (before or {}).get("permission") != after["permission"]
    if level_changed and not str(changed_reason or "").strip():
        raise PermissionValidationError("a freedom change needs a reason")

    if current is not None:
        current.valid_to = start
        session.flush()
    new_row = None
    if any(value is not None for value in after.values()):
        new_row = AgentPermission(
            group_name=group_name,
            fa_id=fa_id,
            changed_by=changed_by,
            changed_reason=changed_reason,
            valid_from=start,
            **after,
        )
        session.add(new_row)
        session.flush()

    record_audit(
        session,
        entity_type=_ENTITY_TYPE,
        entity_id=None if new_row is None else str(new_row.permission_id),
        action="create" if before is None else ("delete" if new_row is None else "update"),
        actor_id=changed_by,
        detail={
            "group_name": group_name,
            "fa_id": fa_id,
            "before": before,
            "after": after,
            "reason": changed_reason,
        },
    )
    return new_row


def rank_members(session: Session, members: Sequence[Any]) -> list[Any]:
    """Clients never enrolled before come first, then the highest risk score."""
    client_ids = {member.client_id for member in members}
    if not client_ids:
        return []
    enrolled = set(
        session.scalars(
            select(Enrollment.client_id).where(Enrollment.client_id.in_(client_ids)).distinct()
        )
    )
    scores = {
        (row.client_id, row.unit_fund_id): row.risk_score
        for row in session.execute(
            select(
                ClientRiskFeatures.client_id,
                ClientRiskFeatures.unit_fund_id,
                ClientRiskFeatures.risk_score,
            ).where(ClientRiskFeatures.client_id.in_(client_ids))
        )
    }
    return sorted(
        members,
        key=lambda m: (m.client_id in enrolled, -scores.get((m.client_id, m.unit_fund_id), 0)),
    )


def apply_group_rules(
    session: Session,
    group_name: str,
    members: Sequence[Any],
    reasons: dict[tuple[int, int], str | None],
    at: date | None = None,
) -> dict[tuple[int, int], str | None]:
    """Add the group's pause, balance range and client limit to the skip reasons."""
    settings = load_group_settings(session, group_name, current_fa_id(), at)
    out = dict(reasons)
    if settings.paused:
        return {key: reason or GROUP_PAUSED for key, reason in out.items()}
    for member in members:
        key = (member.client_id, member.unit_fund_id)
        if out[key] is None and not settings.balance_allows(member.balance):
            out[key] = OUTSIDE_BALANCE_RANGE
    if settings.max_clients_per_day:
        eligible = [m for m in members if out[(m.client_id, m.unit_fund_id)] is None]
        kept: set[int] = set()
        for member in rank_members(session, eligible):
            if len(kept) >= settings.max_clients_per_day:
                break
            kept.add(member.client_id)
        for member in eligible:
            if member.client_id not in kept:
                out[(member.client_id, member.unit_fund_id)] = OVER_GROUP_LIMIT
    return out
