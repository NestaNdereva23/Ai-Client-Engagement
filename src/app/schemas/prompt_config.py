from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class DraftRequest(BaseModel):
    component_key: str = "default"
    rows: list[dict[str, Any]]
    created_by: str | None = None


class DraftOut(BaseModel):
    version: int


class PublishRequest(BaseModel):
    component_key: str = "default"
    by: str | None = None
    at: date | None = None


class DiscardRequest(BaseModel):
    component_key: str = "default"


class VersionSummaryOut(BaseModel):
    version: int
    status: str
    valid_from: date | None
    valid_to: date | None
    created_by: str | None
    published_by: str | None
    published_at: datetime | None
    row_count: int


class DiffOut(BaseModel):
    diff: dict[str, Any]


class ComponentContentOut(BaseModel):
    component_type: str
    component_key: str
    version: int
    rows: list[dict[str, Any]]


class FactEligibilityRowIn(BaseModel):
    fact_field: str
    exposure_mode: str
    condition: str | None = None


class FactEligibilityRowOut(BaseModel):
    fact_field: str
    exposure_mode: str
    condition: str | None = None


class FactEligibilityReplaceRequest(BaseModel):
    rows: list[FactEligibilityRowIn]


class TestGenerateRequest(BaseModel):
    angle: str | None = None
    tier: str | None = None
    angle_version: int | None = None
    tier_version: int | None = None
    voice_version: int | None = None
    safety_version: int | None = None
    output_version: int | None = None
    personalization_version: int | None = None
    client_id: int | None = None
    fact_profile: dict[str, Any] | None = None
    product: str = "money market"
    n: int = 1
    use_rag: bool = True
    cta_override: str | None = None


class TestGenerateRunOut(BaseModel):
    run_id: str
    status: str
    subject: str | None
    body: str | None
    attempts: int
    failed_guardrail: str | None
    reason: str | None


class AngleVersionMetricsOut(BaseModel):
    angle: str
    angle_catalog_version: int | None
    review_count: int
    approval_rate: float
    edit_rate: float
    rejection_rate: float
    regeneration_rate: float


class ExplainFactOut(BaseModel):
    field: str
    used: bool
    value: Any | None


class ExplainOut(BaseModel):
    run_id: str
    angle: str | None
    priority_tier: str | None
    run_kind: str
    rule_version: int | None
    angle_catalog_version: int | None
    tier_contract_version: int | None
    voice_contract_version: int | None
    safety_policy_version: int | None
    output_policy_version: int | None
    personalization_policy_version: int | None
    status: str
    attempts: int
    failed_guardrail: str | None
    reason: str | None
    facts: list[ExplainFactOut]
