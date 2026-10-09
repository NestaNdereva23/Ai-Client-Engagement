from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class MetricWindow(BaseModel):
    since: date
    until: date
    window_days: int


class SpeedBuckets(BaseModel):
    same_day: int
    within_three_days: int
    within_a_week: int
    over_a_week: int


class SpeedOut(BaseModel):
    window: MetricWindow
    sample: int
    average_days: float | None
    median_days: float | None
    p90_days: float | None
    buckets: SpeedBuckets


class CoverageOut(BaseModel):
    window: MetricWindow
    clients_found: int
    contacted_within_a_week: int
    coverage_rate: float | None


class MoneyAtRiskOut(BaseModel):
    window: MetricWindow
    clients_reached: int
    money_reached_kes: float
    deposited_clients: int
    deposited_money_kes: float


class LossesPreventedOut(BaseModel):
    window: MetricWindow
    at_risk_contacted: int
    stayed_clients: int
    money_kept_kes: float


class GrowthFoundOut(BaseModel):
    window: MetricWindow
    introduced_contacted: int
    took_second_fund: int
    money_followed_kes: float


class QualityWeek(BaseModel):
    week_start: date
    decisions: int
    edits: int
    rejections: int
    edit_rate: float | None
    rejection_rate: float | None
    sent: int
    opt_outs: int
    opt_out_rate: float | None
    complaints: int


class QualityOut(BaseModel):
    window: MetricWindow
    weeks: list[QualityWeek]


class DismissReason(BaseModel):
    reason: str
    count: int


class UsefulnessOut(BaseModel):
    window: MetricWindow
    total: int
    accepted: int
    acted_on: int
    dismissed: int
    new: int
    expired: int
    acceptance_rate: float | None
    dismiss_reasons: list[DismissReason]


class FreedomWeek(BaseModel):
    week_start: date
    total: int
    suggest_only: int
    approve_each: int
    approve_sample: int
    act_alone: int


class FreedomOut(BaseModel):
    window: MetricWindow
    weeks: list[FreedomWeek]


class CostOut(BaseModel):
    window: MetricWindow
    finding_count: int
    discovery_cost_kes: float
    cost_per_finding_kes: float | None
    contacted_clients: int
    action_cost_kes: float
    send_spend_kes: float
    cost_per_contacted_kes: float | None
    money_in_kes: float
    total_cost_kes: float
    return_multiple: float | None


class RunDurationStat(BaseModel):
    agent_kind: str
    runs: int
    average_seconds: float | None
    median_seconds: float | None
    p90_seconds: float | None


class LatencyStat(BaseModel):
    sample: int
    p50_ms: float | None
    p90_ms: float | None
    p99_ms: float | None


class BookSize(BaseModel):
    active_client_funds: int
    active_clients: int


class SystemSpeedOut(BaseModel):
    window: MetricWindow
    runs: list[RunDurationStat]
    model_latency: LatencyStat
    book: BookSize


class GuideMixResultLine(BaseModel):
    guide_mix: str
    label: str
    sent: int
    reply_percent: float
    opt_out_percent: float
    edit_percent: float
    deposit_percent: float
    money_in_kes: float


class GuideMixResultsOut(BaseModel):
    window_days: int
    lookback_hours: int
    min_group_size: int
    weeks: int
    lines: list[GuideMixResultLine]
    withheld_small_groups: int


class DashboardOut(BaseModel):
    window: MetricWindow
    speed: SpeedOut
    coverage: CoverageOut
    money_at_risk: MoneyAtRiskOut
    losses_prevented: LossesPreventedOut
    growth_found: GrowthFoundOut
    quality: QualityOut
    usefulness: UsefulnessOut
    freedom: FreedomOut
    cost: CostOut
    system_speed: SystemSpeedOut
