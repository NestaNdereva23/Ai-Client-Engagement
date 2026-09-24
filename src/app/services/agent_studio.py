"""The backend behind Agent Studio: batch simulation, historical comparison,
a read of the configuration in force, and staging the situation-to-action
mapping through the same draft/publish engine every other versioned
component already uses.

Nothing here writes a real proposal. A simulation reads client_situation_state
as it stands and asks what would happen, the same way run_proposal would
decide it, without creating anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action
from app.agents.permissions import effective_permission
from app.agents.propose import GROUP_ACTIONS, SITUATION_PRIORITY
from app.agents.scenario_studio import ACT_ALONE
from app.agents.situation_action_mapping import (
    MappingSpec,
    SituationActionMappingValidationError,
    action_code_for_situation,
    load_active_mappings,
    validate_mappings,
)
from app.agents.watchlist import GROUP_TO_SITUATION, load_thresholds
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_permission import AgentPermission
from app.db.models.signals import ClientSituationSnapshot, ClientSituationState
from app.risk.store import load_active_config
from app.rules import versioning
from app.rules.catalog import load_angle

SITUATION_ACTION_MAPPING = "situation_action_mapping"

SITUATION_CODE_TO_GROUP = {situation: group for group, situation in GROUP_TO_SITUATION.items()}


@dataclass(frozen=True)
class SituationCount:
    situation: str
    client_funds: int
    money_total_kes: float


@dataclass(frozen=True)
class BatchSimulationResult:
    as_of: date
    scanned: int
    matched: int
    multi_match_consolidated: int
    money_total_kes: float
    by_situation: tuple[SituationCount, ...]
    auto_queued: int
    needs_approval: int
    trimmed_by_daily_cap: int


def run_batch_simulation(session: Session, as_of: date) -> BatchSimulationResult:
    """Read tonight's situations as they stand and report what the agent
    would propose today, without writing a single proposal.
    """
    scanned = session.scalar(select(func.count()).select_from(ActiveClientFund)) or 0

    rows = session.execute(
        select(
            ClientSituationState.client_id,
            ClientSituationState.unit_fund_id,
            ClientSituationState.situation_code,
            ActiveClientFund.balance,
        )
        .join(
            ActiveClientFund,
            (ActiveClientFund.client_id == ClientSituationState.client_id)
            & (ActiveClientFund.unit_fund_id == ClientSituationState.unit_fund_id),
        )
        .where(ClientSituationState.is_active.is_(True))
    ).all()

    by_key: dict[tuple[int, int], dict[str, object]] = {}
    for row in rows:
        key = (row.client_id, row.unit_fund_id)
        entry = by_key.setdefault(key, {"groups": [], "balance": float(row.balance or 0.0)})
        group_name = SITUATION_CODE_TO_GROUP.get(row.situation_code)
        if group_name is not None:
            entry["groups"].append(group_name)

    matched = sum(1 for entry in by_key.values() if entry["groups"])
    multi_match = sum(1 for entry in by_key.values() if len(entry["groups"]) > 1)

    winners: dict[str, list[float]] = {name: [] for name in SITUATION_PRIORITY}
    for entry in by_key.values():
        groups = entry["groups"]
        for situation in SITUATION_PRIORITY:
            if situation in groups:
                winners[situation].append(entry["balance"])
                break

    by_situation = tuple(
        SituationCount(
            situation=situation, client_funds=len(balances), money_total_kes=sum(balances)
        )
        for situation, balances in winners.items()
    )
    money_total = sum(count.money_total_kes for count in by_situation)

    auto_queued = 0
    needs_approval = 0

    for situation, balances in winners.items():
        if not balances:
            continue
        client_count = len(balances)
        money = sum(balances)
        situation_code = GROUP_TO_SITUATION.get(situation, situation)
        action_code = action_code_for_situation(
            session, situation_code, as_of
        ) or GROUP_ACTIONS.get(situation)
        if action_code is None:
            needs_approval += client_count
            continue
        action = load_action(session, action_code, as_of)
        ceiling = action.money_ceiling_kes if action is not None else None
        permission = effective_permission(
            session, action_code, money_total_kes=money, money_ceiling_kes=ceiling
        )
        if permission != ACT_ALONE:
            needs_approval += client_count
            continue

        auto_queued += client_count

    return BatchSimulationResult(
        as_of=as_of,
        scanned=scanned,
        matched=matched,
        multi_match_consolidated=multi_match,
        money_total_kes=money_total,
        by_situation=by_situation,
        auto_queued=auto_queued,
        needs_approval=needs_approval,
        trimmed_by_daily_cap=0,
    )


@dataclass(frozen=True)
class SituationCompareRow:
    situation: str
    count_a: int
    count_b: int


@dataclass(frozen=True)
class SnapshotComparison:
    date_a: date
    date_b: date
    rows: tuple[SituationCompareRow, ...]


def _situation_counts_as_of(session: Session, cutoff: datetime) -> dict[str, int]:
    latest = (
        select(
            ClientSituationSnapshot.situation_code,
            ClientSituationSnapshot.is_active,
        )
        .distinct(
            ClientSituationSnapshot.client_id,
            ClientSituationSnapshot.unit_fund_id,
            ClientSituationSnapshot.situation_code,
        )
        .where(ClientSituationSnapshot.created_at <= cutoff)
        .order_by(
            ClientSituationSnapshot.client_id,
            ClientSituationSnapshot.unit_fund_id,
            ClientSituationSnapshot.situation_code,
            ClientSituationSnapshot.created_at.desc(),
        )
        .subquery()
    )
    rows = session.execute(
        select(latest.c.situation_code, func.count())
        .where(latest.c.is_active.is_(True))
        .group_by(latest.c.situation_code)
    ).all()
    return dict(rows)


def compare_situation_snapshots(session: Session, date_a: date, date_b: date) -> SnapshotComparison:
    """How the population under each situation has changed between two dates,
    both read under whatever rules were live on that date.
    """
    counts_a = _situation_counts_as_of(session, datetime.combine(date_a, time.max))
    counts_b = _situation_counts_as_of(session, datetime.combine(date_b, time.max))
    rows = tuple(
        SituationCompareRow(
            situation=group_name,
            count_a=counts_a.get(situation_code, 0),
            count_b=counts_b.get(situation_code, 0),
        )
        for situation_code, group_name in SITUATION_CODE_TO_GROUP.items()
    )
    return SnapshotComparison(date_a=date_a, date_b=date_b, rows=rows)


@dataclass(frozen=True)
class PermissionSetting:
    action_code: str
    priority_tier: str | None
    risk_band: str | None
    permission: str
    max_clients_per_day: int | None
    max_money_kes: float | None


@dataclass(frozen=True)
class ConfigurationSummary:
    as_of: date
    risk_config_version: int | None
    small_balance_kes: float
    months_until_empty: float
    awaiting_call_days: int
    new_client_days: int
    situation_priority: tuple[str, ...]
    daily_send_limit: int | None
    first_run_limit: int
    kill_switch_active: bool
    permissions: tuple[PermissionSetting, ...]


def current_configuration(session: Session, as_of: date) -> ConfigurationSummary:
    thresholds = load_thresholds(session, as_of)
    config = load_active_config(session, as_of)
    settings = get_settings()
    permission_rows = session.scalars(select(AgentPermission)).all()
    return ConfigurationSummary(
        as_of=as_of,
        risk_config_version=config.version if config is not None else None,
        small_balance_kes=thresholds.small_balance,
        months_until_empty=thresholds.months_until_empty,
        awaiting_call_days=thresholds.awaiting_call_days,
        new_client_days=thresholds.new_client_days,
        situation_priority=SITUATION_PRIORITY,
        daily_send_limit=settings.agent_daily_send_limit,
        first_run_limit=settings.agent_first_run_limit,
        kill_switch_active=settings.agent_force_approve_each,
        permissions=tuple(
            PermissionSetting(
                action_code=row.action_code,
                priority_tier=row.priority_tier,
                risk_band=row.risk_band,
                permission=row.permission,
                max_clients_per_day=row.max_clients_per_day,
                max_money_kes=row.max_money_kes,
            )
            for row in permission_rows
        ),
    )


def configuration_versions(session: Session) -> list[dict]:
    """Every risk_config_version, live or draft, through the generic engine."""
    return versioning.list_versions(
        session, "risk_config_version", versioning.DEFAULT_COMPONENT_KEY
    )


def compare_configuration_versions(session: Session, version_a: int, version_b: int) -> dict:
    return versioning.diff_versions(
        session, "risk_config_version", versioning.DEFAULT_COMPONENT_KEY, version_a, version_b
    )


def current_situation_mapping(session: Session, as_of: date) -> list:
    return load_active_mappings(session, as_of)


def situation_mapping_versions(session: Session) -> list[dict]:
    return versioning.list_versions(
        session, SITUATION_ACTION_MAPPING, versioning.DEFAULT_COMPONENT_KEY
    )


def situation_mapping_pending_version(session: Session) -> int | None:
    drafts = [
        summary["version"]
        for summary in situation_mapping_versions(session)
        if summary["status"] == "draft"
    ]
    return max(drafts) if drafts else None


def angle_brief(session: Session, angle: str, as_of: date):
    return load_angle(session, angle, as_of)


def stage_situation_mapping_draft(
    session: Session, mappings: Sequence[MappingSpec], *, by: str | None
) -> int:
    """Validate a full replacement mapping and stage it as an unpublished draft."""
    try:
        validate_mappings(mappings)
    except SituationActionMappingValidationError as exc:
        raise SituationActionMappingValidationError(str(exc)) from exc

    rows = [
        {
            "situation": spec.situation,
            "action_code": spec.action_code,
            "objective": spec.objective,
            "angle": spec.angle,
            "evidence_required": spec.evidence_required,
            "channel": spec.channel,
        }
        for spec in mappings
    ]
    return versioning.save_draft(
        session, SITUATION_ACTION_MAPPING, versioning.DEFAULT_COMPONENT_KEY, rows, by=by
    )


def publish_situation_mapping_draft(
    session: Session, version: int, *, by: str | None, at: date | None = None
) -> None:
    versioning.publish(
        session, SITUATION_ACTION_MAPPING, versioning.DEFAULT_COMPONENT_KEY, version, by=by, at=at
    )


def discard_situation_mapping_draft(session: Session, version: int) -> None:
    versioning.discard_draft(
        session, SITUATION_ACTION_MAPPING, versioning.DEFAULT_COMPONENT_KEY, version
    )
