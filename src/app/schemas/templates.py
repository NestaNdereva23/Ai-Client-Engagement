"""Request and response shapes for the template review and instantiation API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.review import BatchReviewOutcome, OutreachMessageSummary, ReviewOutcome


class DecideTemplateRequest(BaseModel):
    """One reviewer decision on one template.

    reviewer_id is not a field here: the reviewer is the caller
    authenticated by X-Reviewer-Key (app.api.reviewer_auth), not whatever
    a request body claims.
    """

    outcome: ReviewOutcome
    reason: str | None = None
    edited_content: dict | None = None

    @model_validator(mode="after")
    def _edit_approve_needs_content(self) -> DecideTemplateRequest:
        if self.outcome == "edit_approve" and not self.edited_content:
            raise ValueError("edit_approve requires edited_content")
        return self


class DecideBatchTemplateRequest(BaseModel):
    """One reviewer decision applied to a list of templates at once.

    Same reviewer_id and edited_content rules as DecideTemplateRequest: the
    reviewer comes from X-Reviewer-Key, and edit_approve isn't offered
    here since an edit is inherently per-template.
    """

    template_ids: list[str] = Field(min_length=1, max_length=500)
    outcome: BatchReviewOutcome
    reason: str | None = None


class MessageTemplateSummary(BaseModel):
    """One queue row: enough to list and pick a template to open."""

    model_config = ConfigDict(from_attributes=True)

    template_id: str
    campaign_id: int
    status: str
    profile_key: dict
    created_at: datetime


class TemplateReviewActionOut(BaseModel):
    """One recorded template decision, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    review_action_id: int
    reviewer_id: str
    outcome: str
    edited_content: dict | None
    message_angle: str | None
    priority_tier: str | None
    edit_diff: dict | None
    reason: str | None
    created_at: datetime


class DecideBatchTemplateFailureOut(BaseModel):
    """One template a batch decide call skipped, and why."""

    model_config = ConfigDict(from_attributes=True)

    template_id: str
    error: str


class DecideBatchTemplateResultOut(BaseModel):
    """What one decide-batch call did: one review action per template that
    decided cleanly, and one failure entry per template that didn't.
    """

    decided: list[TemplateReviewActionOut]
    failed: list[DecideBatchTemplateFailureOut]


class MessageTemplateDetail(MessageTemplateSummary):
    """A template with its draft and full decision history."""

    generation_run_id: str
    ai_draft_content: dict
    updated_at: datetime
    history: list[TemplateReviewActionOut]


class InstantiateTemplateResult(BaseModel):
    """How many messages one instantiation call produced, and which."""

    instantiated_count: int
    messages: list[OutreachMessageSummary]


class ProfileKeyOut(BaseModel):
    """The shared, profile-defining facts one estimated bucket's clients have in common."""

    message_angle: str
    priority_tier: str | None
    product: str
    has_cadence: bool
    stale_contact: bool
    exit_reason_charge_settled: bool
    fund_name_known: bool


class BucketEstimateOut(BaseModel):
    """One profile's worth of due, eligible clients, and how many of them there are."""

    profile_key: ProfileKeyOut
    client_count: int


class EstimateComputedFromOut(BaseModel):
    """The inputs behind an estimate, so "same configuration, same number" is checkable."""

    limit: int
    as_of: datetime


class TemplateEstimateOut(BaseModel):
    """How many templates drafting this campaign right now would produce.

    Three separate numbers, never conflated: estimated_templates here,
    the configured maximum from GET .../templates/policy, and actual
    generated from a real drafting call.
    """

    estimated_templates: int
    eligible_clients: int
    buckets: list[BucketEstimateOut]
    computed_from: EstimateComputedFromOut


class TemplatePolicyRequest(BaseModel):
    """A campaign manager's own override for how many templates one
    drafting call may produce. Either field, or both, or neither -- neither
    set means no limit.
    """

    max_templates: int | None = None
    max_templates_pct: int | None = None
    updated_by: str


class TemplatePolicyOut(BaseModel):
    """The limit in force for one campaign: its own override if it has set
    one, otherwise the active system default.
    """

    source: str
    max_templates: int | None
    max_templates_pct: int | None
    updated_at: datetime | None
    updated_by: str | None


class DraftTemplatesResult(BaseModel):
    """What one drafting call produced, and the plan behind it.

    Three numbers, never conflated: estimated_templates (this call's due
    cohort, before any skip or limit), effective_limit (the cap the policy
    in force resolved to, or null for no limit), and drafted_count (what
    actually landed). skipped_existing counts buckets a previous call
    already templated; failed_guardrails counts ones this call attempted
    but every guardrail retry rejected; failed_errors counts ones an
    unexpected error interrupted before a guardrail verdict was ever
    reached, and which are picked up again the next call.
    """

    estimated_templates: int
    effective_limit: int | None
    drafted_count: int
    skipped_existing: int
    failed_guardrails: int
    failed_errors: int
    policy: TemplatePolicyOut
    templates: list[MessageTemplateSummary]
