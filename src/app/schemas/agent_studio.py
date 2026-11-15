from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ScenarioRequest(BaseModel):
    balance: float
    days_since_last_deposit: int | None = None
    typical_gap_days: float | None = None
    real_withdrawer: bool = False
    net_flow_negative: bool = False
    funds_held: int = 1
    n_deposits: int = 1
    first_deposit_days_ago: int | None = None
    sig_shrinking: bool = False
    advisor_call_overdue: bool = False
    risk_band_rose: bool = False
    near_preservation_line: bool = False
    new_client_days: int | None = None
    money_ceiling_kes: float | None = None
    as_of: date | None = None


class ScenarioResponseOut(BaseModel):
    matched: tuple[str, ...]
    winner: str | None
    folded: tuple[str, ...]
    action_code: str | None
    action_title: str | None
    permission: str | None
    outcome: str
    reason: str


class SituationCountOut(BaseModel):
    situation: str
    client_funds: int
    money_total_kes: float


class BatchSimulationOut(BaseModel):
    as_of: date
    scanned: int
    matched: int
    multi_match_consolidated: int
    money_total_kes: float
    by_situation: tuple[SituationCountOut, ...]
    auto_queued: int
    needs_approval: int
    trimmed_by_daily_cap: int


class SituationCompareRowOut(BaseModel):
    situation: str
    count_a: int
    count_b: int


class SnapshotComparisonOut(BaseModel):
    date_a: date
    date_b: date
    rows: tuple[SituationCompareRowOut, ...]


class PermissionSettingOut(BaseModel):
    action_code: str
    priority_tier: str | None
    risk_band: str | None
    permission: str
    max_clients_per_day: int | None
    max_money_kes: float | None


class ConfigurationSummaryOut(BaseModel):
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
    permissions: tuple[PermissionSettingOut, ...]


class ConfigVersionSummaryOut(BaseModel):
    version: int
    status: str
    valid_from: date | None
    valid_to: date | None
    created_by: str | None
    published_by: str | None


class ConfigVersionDiffOut(BaseModel):
    fields: dict[str, tuple[object | None, object | None]]


class SituationMappingRowOut(BaseModel):
    situation: str
    action_code: str
    objective: str
    angle: str
    evidence_required: str
    channel: str


class SituationMappingOut(BaseModel):
    version: int | None
    rows: tuple[SituationMappingRowOut, ...]
    pending_version: int | None


class AngleBriefOut(BaseModel):
    angle: str
    headline: str
    who: str
    claim: str
    ask: str
    never: str
    use: str
    held: bool


class SituationMappingRowIn(BaseModel):
    situation: str
    action_code: str
    objective: str
    angle: str
    evidence_required: str
    channel: str


class SituationMappingDraftRequest(BaseModel):
    rows: list[SituationMappingRowIn] = Field(min_length=1)


class SituationMappingDraftOut(BaseModel):
    version: int


class SituationMappingPublishRequest(BaseModel):
    at: date | None = None
