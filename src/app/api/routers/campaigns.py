"""Campaign console: list, create, enrollment summary, sequence steps, and
triggering generation for whatever is due.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.email_channel import build_default_agent
from app.campaigns.batch_generation import BatchNotFound
from app.campaigns.estimation import DEFAULT_ESTIMATE_LIMIT, MAX_ESTIMATE_LIMIT
from app.campaigns.generation import model_boundary_audit_sink
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.template_policy import EffectivePolicy, TemplatePolicyValidationError
from app.config import get_settings
from app.db.session import get_session
from app.llmops.tracing import get_shared_tracer
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.privacy.llm_client import get_llm_client
from app.schemas.campaigns import (
    BatchIngestOutcomeOut,
    BatchIngestResultOut,
    CampaignCreateOut,
    CampaignCreateRequest,
    CampaignListItemOut,
    CampaignStepCreateRequest,
    CampaignStepOut,
    CampaignSummaryOut,
    GenerationBatchOut,
    TouchOutcomeOut,
)
from app.schemas.review import OutreachMessageSummary
from app.schemas.templates import (
    BucketEstimateOut,
    DraftTemplatesResult,
    EstimateComputedFromOut,
    InstantiateTemplateResult,
    MessageTemplateSummary,
    ProfileKeyOut,
    TemplateEstimateOut,
    TemplatePolicyOut,
    TemplatePolicyRequest,
)
from app.services.campaigns import (
    CampaignNotFound,
    add_campaign_step,
    campaign_summary,
    create_campaign,
    draft_campaign_templates,
    estimate_campaign_templates,
    get_campaign_batch,
    get_campaign_template_policy,
    ingest_campaign_batch,
    instantiate_campaign_template,
    list_campaigns,
    run_campaign_generation,
    set_campaign_template_policy,
    submit_campaign_batch,
)
from app.services.review import TemplateNotApproved
from app.services.template_review import TemplateNotFound

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("", response_model=Page[CampaignListItemOut])
def get_campaigns(
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[CampaignListItemOut]:
    """Campaigns oldest-first, each carrying its own enrollment counts."""
    try:
        rows, next_cursor = list_campaigns(session, status=status, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    items = [
        CampaignListItemOut(
            campaign_id=r.campaign_id,
            name=r.name,
            campaign_type=r.campaign_type,
            status=r.status,
            cohort_definition=r.cohort_definition,
            start_date=r.start_date,
            end_date=r.end_date,
            created_at=r.created_at,
            total_enrolled=r.total_enrolled,
            primary_count=r.primary_count,
            suppressed_count=r.total_enrolled - r.primary_count,
        )
        for r in rows
    ]
    return Page(items=items, next_cursor=next_cursor)


@router.post("", response_model=CampaignCreateOut, status_code=201)
def post_campaign(
    body: CampaignCreateRequest, session: Session = Depends(get_session)
) -> CampaignCreateOut:
    """Create a campaign and enroll every client currently matching its cohort filter."""
    campaign, enrolled_count = create_campaign(
        session,
        name=body.name,
        campaign_type=body.campaign_type,
        cohort_filters=body.cohort.model_dump(),
        start_date=body.start_date,
        end_date=body.end_date,
    )
    session.commit()
    return CampaignCreateOut(
        campaign_id=campaign.campaign_id,
        name=campaign.name,
        campaign_type=campaign.campaign_type,
        status=campaign.status,
        cohort_definition=campaign.cohort_definition,
        start_date=campaign.start_date,
        end_date=campaign.end_date,
        created_at=campaign.created_at,
        enrolled_count=enrolled_count,
    )


@router.get("/{campaign_id}/summary", response_model=CampaignSummaryOut)
def get_campaign_summary(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignSummaryOut:
    """Enrollment counts for one campaign, including rows suppressed as a duplicate person."""
    try:
        summary = campaign_summary(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignSummaryOut(campaign_id=campaign_id, **summary)


@router.post("/{campaign_id}/steps", response_model=CampaignStepOut, status_code=201)
def post_campaign_step(
    campaign_id: int,
    body: CampaignStepCreateRequest,
    session: Session = Depends(get_session),
) -> CampaignStepOut:
    """Append the next step in a campaign's send sequence.

    A campaign with no steps is enrolled but permanently idle: the
    eligibility gate refuses to generate a step that has no CampaignStep
    row, so this is required before /generate can do anything.
    """
    try:
        step = add_campaign_step(
            session,
            campaign_id,
            offset_days=body.offset_days,
            message_angle=body.message_angle,
            template_ref=body.template_ref,
        )
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignStepOut(
        step_id=step.step_id,
        campaign_id=step.campaign_id,
        step_no=step.step_no,
        offset_days=step.offset_days,
        message_angle=step.message_angle,
        template_ref=step.template_ref,
    )


@router.post("/{campaign_id}/generate", response_model=list[TouchOutcomeOut])
def post_campaign_generate(
    campaign_id: int,
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=DEFAULT_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> list[TouchOutcomeOut]:
    """Generate a touch for every one of this campaign's due, eligible
    enrollments. Nothing sends: an eligible enrollment ends this call as a
    pending_review message, exactly where the review queue picks it up.

    Every enrollment in the batch is a full model run, so a large cohort
    holds the request open for as long as that takes. limit caps how many
    are attempted in one call; whatever is left stays due and is picked up
    by the next one, so a cohort can be worked through a batch at a time.
    """
    tracer = get_shared_tracer()
    agent = build_default_agent(session, audit=model_boundary_audit_sink(session), tracer=tracer)
    try:
        outcomes = run_campaign_generation(
            session,
            campaign_id,
            agent=agent,
            settings=get_settings(),
            tracer=tracer,
            limit=limit,
        )
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return [
        TouchOutcomeOut(
            enrollment_id=o.enrollment_id,
            generated=o.generated,
            reason=o.reason,
            touch_id=o.touch_id,
        )
        for o in outcomes
    ]


def _batch_out(batch) -> GenerationBatchOut:
    return GenerationBatchOut(
        generation_batch_id=batch.generation_batch_id,
        campaign_id=batch.campaign_id,
        provider=batch.provider,
        provider_batch_id=batch.provider_batch_id,
        status=batch.status,
        requested_limit=batch.requested_limit,
        requested_count=batch.requested_count,
        succeeded_count=batch.succeeded_count,
        errored_count=batch.errored_count,
        submitted_at=batch.submitted_at,
        ended_at=batch.ended_at,
        ingested_at=batch.ingested_at,
        created_at=batch.created_at,
    )


@router.post("/{campaign_id}/generate/batch", response_model=GenerationBatchOut, status_code=201)
def post_campaign_generate_batch(
    campaign_id: int,
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=DEFAULT_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> GenerationBatchOut:
    """Submit this campaign's due, eligible enrollments to the model
    provider's batch endpoint in one call, instead of drafting each one
    synchronously. Nothing is reviewable yet: the provider drafts the whole
    cohort off the request path, and POST .../batches/{id}/ingest turns the
    results into pending-review messages once it reports the batch ended.

    limit caps how many enrollments this one submission can include, the
    same knob /generate already exposes; whatever is left over stays due
    for the next call, either to /generate or to this endpoint again.
    """
    try:
        batch = submit_campaign_batch(
            session,
            campaign_id,
            settings=get_settings(),
            limit=limit,
            tracer=get_shared_tracer(),
        )
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return _batch_out(batch)


@router.get("/{campaign_id}/batches/{generation_batch_id}", response_model=GenerationBatchOut)
def get_campaign_batch_status(
    campaign_id: int, generation_batch_id: str, session: Session = Depends(get_session)
) -> GenerationBatchOut:
    """One batch submission's current state: still with the provider, ended
    and waiting to be ingested, or already turned into review-queue messages.
    """
    try:
        batch = get_campaign_batch(session, campaign_id, generation_batch_id)
    except BatchNotFound:
        raise HTTPException(status_code=404, detail="batch not found") from None
    return _batch_out(batch)


@router.post(
    "/{campaign_id}/batches/{generation_batch_id}/ingest", response_model=BatchIngestResultOut
)
def post_campaign_batch_ingest(
    campaign_id: int, generation_batch_id: str, session: Session = Depends(get_session)
) -> BatchIngestResultOut:
    """Check the provider for this batch's results and, once it reports the
    batch ended, turn each result into the same pending-review message the
    synchronous /generate path produces. Safe to call before the provider
    is done -- it just returns the batch's current status with no outcomes
    -- and safe to call again after ingestion, which is a no-op the second
    time.
    """
    tracer = get_shared_tracer()
    try:
        result = ingest_campaign_batch(
            session, campaign_id, generation_batch_id, settings=get_settings(), tracer=tracer
        )
        session.commit()
    except BatchNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="batch not found") from None
    return BatchIngestResultOut(
        batch=_batch_out(result.batch),
        outcomes=[
            BatchIngestOutcomeOut(custom_id=o.custom_id, status=o.status, reason=o.reason)
            for o in result.outcomes
        ],
    )


def _policy_out(policy: EffectivePolicy) -> TemplatePolicyOut:
    return TemplatePolicyOut(
        source=policy.source,
        max_templates=policy.max_templates,
        max_templates_pct=policy.max_templates_pct,
        updated_at=policy.updated_at,
        updated_by=policy.updated_by,
    )


@router.get("/{campaign_id}/templates/estimate", response_model=TemplateEstimateOut)
def get_campaign_templates_estimate(
    campaign_id: int,
    limit: int = Query(default=DEFAULT_ESTIMATE_LIMIT, ge=1, le=MAX_ESTIMATE_LIMIT),
    session: Session = Depends(get_session),
) -> TemplateEstimateOut:
    """How many distinct templates this campaign's current configuration
    would produce. Deterministic given the same due cohort, never
    constructs an LLMClient, and changes nothing -- safe to call any time,
    including before /templates/draft to see what a limit would bite into.
    """
    try:
        estimate = estimate_campaign_templates(session, campaign_id, limit=limit)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return TemplateEstimateOut(
        estimated_templates=estimate.estimated_templates,
        eligible_clients=estimate.eligible_clients,
        buckets=[
            BucketEstimateOut(
                profile_key=ProfileKeyOut(**bucket.profile_key.as_dict()),
                client_count=bucket.client_count,
            )
            for bucket in estimate.buckets
        ],
        computed_from=EstimateComputedFromOut(limit=estimate.limit, as_of=estimate.as_of),
    )


@router.get("/{campaign_id}/templates/policy", response_model=TemplatePolicyOut)
def get_campaign_templates_policy(
    campaign_id: int, session: Session = Depends(get_session)
) -> TemplatePolicyOut:
    """The limit in force for this campaign right now: its own override if
    it has set one, otherwise the active system default.
    """
    try:
        policy = get_campaign_template_policy(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return _policy_out(policy)


@router.put("/{campaign_id}/templates/policy", response_model=TemplatePolicyOut)
def put_campaign_templates_policy(
    campaign_id: int, body: TemplatePolicyRequest, session: Session = Depends(get_session)
) -> TemplatePolicyOut:
    """Set this campaign's own template generation limit.

    A limit is a throttle, not a decision about the campaign: raising it
    and calling /templates/draft again tops up rather than redrafting, and
    lowering it deletes nothing already drafted.
    """
    try:
        policy = set_campaign_template_policy(
            session,
            campaign_id,
            max_templates=body.max_templates,
            max_templates_pct=body.max_templates_pct,
            updated_by=body.updated_by,
        )
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except TemplatePolicyValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _policy_out(policy)


@router.post("/{campaign_id}/templates/draft", response_model=DraftTemplatesResult)
def post_campaign_templates_draft(
    campaign_id: int,
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=DEFAULT_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> DraftTemplatesResult:
    """Group this campaign's due, eligible enrollments into buckets and draft
    one template per bucket -- a third path alongside /generate and
    /generate/batch. Nothing is instantiated yet: each template needs its
    own review at GET/POST /templates first.
    """
    try:
        templates = draft_campaign_templates(
            session,
            campaign_id,
            settings=get_settings(),
            llm_client=get_llm_client(get_settings()),
            limit=limit,
            tracer=get_shared_tracer(),
        )
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return DraftTemplatesResult(
        drafted_count=len(templates),
        templates=[MessageTemplateSummary.model_validate(t) for t in templates],
    )


@router.post(
    "/{campaign_id}/templates/{template_id}/instantiate",
    response_model=InstantiateTemplateResult,
)
def post_campaign_template_instantiate(
    campaign_id: int,
    template_id: str,
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=DEFAULT_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> InstantiateTemplateResult:
    """Fill in every due, eligible client currently matching an approved
    template's profile, each as its own outreach_message (pending_review or
    auto-approved, per this tier's sampling policy). Safe to call again
    later: an already-instantiated client is skipped, and one who becomes
    due afterwards is picked up then.
    """
    try:
        messages = instantiate_campaign_template(session, campaign_id, template_id, limit=limit)
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except TemplateNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="template not found") from None
    except TemplateNotApproved:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="template is not approved; nothing to instantiate"
        ) from None
    return InstantiateTemplateResult(
        instantiated_count=len(messages),
        messages=[OutreachMessageSummary.model_validate(m) for m in messages],
    )
