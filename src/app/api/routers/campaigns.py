from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.email_channel import build_default_agent
from app.api.reviewer_auth import get_current_reviewer_id
from app.campaigns.batch_generation import BatchNotFound
from app.campaigns.estimation import DEFAULT_ESTIMATE_LIMIT, MAX_ESTIMATE_LIMIT
from app.campaigns.generation import model_boundary_audit_sink
from app.campaigns.generation_cost import (
    DEFAULT_MODEL,
    MODEL_LABELS,
    GenerationCostConfigMissing,
    UnknownGenerationModel,
)
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT, MAX_BATCH_LIMIT
from app.campaigns.template_policy import EffectivePolicy, TemplatePolicyValidationError
from app.config import get_settings
from app.db.session import get_session
from app.llmops.tracing import get_shared_tracer
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.privacy.llm_client import get_llm_client
from app.schemas.campaigns import (
    BatchIngestOutcomeOut,
    BatchIngestResultOut,
    CampaignBatchCreateOut,
    CampaignBatchCreateRequest,
    CampaignBatchFailureOut,
    CampaignCreateOut,
    CampaignCreateRequest,
    CampaignDetailOut,
    CampaignListItemOut,
    CampaignReadinessOut,
    CampaignStepCreateRequest,
    CampaignStepOut,
    CampaignSummaryOut,
    CampaignValueOut,
    CohortFilter,
    CohortPreviewAngleOut,
    CohortPreviewBatchOut,
    CohortPreviewBatchRequest,
    CohortPreviewNarrowOut,
    CohortPreviewOut,
    EnrollmentOut,
    GenerationBatchOut,
    GenerationCostModelOut,
    GenerationCostOut,
    GenerationCostScenarioOut,
    OutreachAnalyticsOut,
    OutreachBucketOut,
    OutreachTrendOut,
    OutreachTrendPointOut,
    TouchOutcomeOut,
    TouchSendOutcomeOut,
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
    NonIncreasingStepOffset,
    add_campaign_step,
    campaign_readiness,
    campaign_summary,
    campaign_value,
    create_campaign,
    create_campaign_batch,
    draft_campaign_templates,
    estimate_campaign_generation_cost,
    estimate_campaign_templates,
    get_campaign,
    get_campaign_batch,
    get_campaign_template_policy,
    ingest_campaign_batch,
    instantiate_campaign_template,
    list_campaign_batches,
    list_campaign_enrollments,
    list_campaign_steps,
    list_campaigns,
    list_generation_cost_models,
    outreach_analytics,
    outreach_trend,
    preview_cohort,
    preview_cohort_batch,
    run_campaign_generation,
    send_campaign,
    set_campaign_template_policy,
    submit_campaign_batch,
)
from app.services.review import TemplateNotApproved
from app.services.template_review import TemplateNotFound
from app.workers.batch_ingest import poll_batch_until_done

router = APIRouter(
    prefix="/campaigns", tags=["campaigns"], dependencies=[Depends(get_current_reviewer_id)]
)


@router.get("", response_model=Page[CampaignListItemOut])
def get_campaigns(
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    newly_dormant: bool | None = None,
    message_angle: str | None = None,
    session: Session = Depends(get_session),
) -> Page[CampaignListItemOut]:
    try:
        rows, next_cursor = list_campaigns(
            session,
            status=status,
            cursor=cursor,
            limit=limit,
            fund_id=fund_id,
            value_band=value_band,
            recency_band=recency_band,
            purchase_depth=purchase_depth,
            newly_dormant=newly_dormant,
            message_angle=message_angle,
        )
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


@router.post("/batch", response_model=CampaignBatchCreateOut, status_code=201)
def post_campaign_batch_create(
    body: CampaignBatchCreateRequest, session: Session = Depends(get_session)
) -> CampaignBatchCreateOut:
    result = create_campaign_batch(
        session,
        name=body.name,
        campaign_type=body.campaign_type,
        shared_cohort_filters=body.cohort.model_dump(),
        angles=body.angles,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    session.commit()
    return CampaignBatchCreateOut(
        created=[
            CampaignCreateOut(
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
            for campaign, enrolled_count in result.created
        ],
        failed=[
            CampaignBatchFailureOut(angle=failure.angle, error=failure.error)
            for failure in result.failed
        ],
    )


@router.post("/preview", response_model=CohortPreviewOut)
def post_campaign_preview(
    body: CohortFilter, session: Session = Depends(get_session)
) -> CohortPreviewOut:
    preview = preview_cohort(session, body.model_dump())
    return CohortPreviewOut(
        matched_count=preview.matched_count,
        primary_count=preview.primary_count,
        suppressed_count=preview.suppressed_count,
        valued_count=preview.valued_count,
        estimated_value=preview.estimated_value,
    )


@router.post("/preview/batch", response_model=CohortPreviewBatchOut)
def post_campaign_preview_batch(
    body: CohortPreviewBatchRequest, session: Session = Depends(get_session)
) -> CohortPreviewBatchOut:
    narrow_filters = {
        "fund_id": body.fund_id,
        "value_band": body.value_band,
        "recency_band": body.recency_band,
        "purchase_depth": body.purchase_depth,
        "newly_dormant": body.newly_dormant,
    }
    result = preview_cohort_batch(session, narrow_filters, body.angles)
    return CohortPreviewBatchOut(
        narrow=CohortPreviewNarrowOut(
            matched_count=result.narrow.matched_count,
            estimated_value=result.narrow.estimated_value,
        ),
        angles=[
            CohortPreviewAngleOut(
                message_angle=a.message_angle,
                matched_count=a.matched_count,
                estimated_value=a.estimated_value,
            )
            for a in result.angles
        ],
    )


@router.get("/analytics", response_model=OutreachAnalyticsOut)
def get_campaigns_analytics(session: Session = Depends(get_session)) -> OutreachAnalyticsOut:
    analytics = outreach_analytics(session)
    return OutreachAnalyticsOut(
        total_enrolled=analytics.total_enrolled,
        primary_count=analytics.primary_count,
        suppressed_count=analytics.suppressed_count,
        active_campaign_count=analytics.active_campaign_count,
        by_enrollment_status=[
            OutreachBucketOut(key=k, count=c) for k, c in analytics.by_enrollment_status
        ],
        by_value_band=[OutreachBucketOut(key=k, count=c) for k, c in analytics.by_value_band],
        by_recency_band=[OutreachBucketOut(key=k, count=c) for k, c in analytics.by_recency_band],
        by_priority_tier=[OutreachBucketOut(key=k, count=c) for k, c in analytics.by_priority_tier],
        by_message_angle=[OutreachBucketOut(key=k, count=c) for k, c in analytics.by_message_angle],
        by_message_status=[
            OutreachBucketOut(key=k, count=c) for k, c in analytics.by_message_status
        ],
        by_review_outcome=[
            OutreachBucketOut(key=k, count=c) for k, c in analytics.by_review_outcome
        ],
        by_contact_event=[OutreachBucketOut(key=k, count=c) for k, c in analytics.by_contact_event],
        reengaged_count=analytics.reengaged_count,
        reengagement_rate=analytics.reengagement_rate,
    )


@router.get("/analytics/trend", response_model=OutreachTrendOut)
def get_campaigns_analytics_trend(
    days: int = Query(default=30, ge=1, le=90),
    session: Session = Depends(get_session),
) -> OutreachTrendOut:
    points = outreach_trend(session, days=days)
    return OutreachTrendOut(
        points=[
            OutreachTrendPointOut(
                day=p.day, touches_sent=p.touches_sent, replies=p.replies, bounces=p.bounces
            )
            for p in points
        ]
    )


@router.get("/generation-cost/models", response_model=list[GenerationCostModelOut])
def get_generation_cost_models(
    session: Session = Depends(get_session),
) -> list[GenerationCostModelOut]:
    configs = list_generation_cost_models(session)
    return [
        GenerationCostModelOut(
            model=c.model,
            label=MODEL_LABELS[c.model],
            config_version=c.version,
            rate_per_generation_usd=c.cost_per_generation_usd,
            rate_per_generation_kes=c.cost_per_generation_kes,
        )
        for c in configs
    ]


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign_detail(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignDetailOut:
    try:
        campaign = get_campaign(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignDetailOut(
        campaign_id=campaign.campaign_id,
        name=campaign.name,
        campaign_type=campaign.campaign_type,
        status=campaign.status,
        cohort_definition=campaign.cohort_definition,
        start_date=campaign.start_date,
        end_date=campaign.end_date,
        created_at=campaign.created_at,
    )


@router.get("/{campaign_id}/summary", response_model=CampaignSummaryOut)
def get_campaign_summary(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignSummaryOut:
    try:
        summary = campaign_summary(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignSummaryOut(campaign_id=campaign_id, **summary)


@router.get("/{campaign_id}/value", response_model=CampaignValueOut)
def get_campaign_value(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignValueOut:
    try:
        value = campaign_value(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignValueOut(campaign_id=campaign_id, **value)


@router.get("/{campaign_id}/generation-cost", response_model=GenerationCostOut)
def get_campaign_generation_cost(
    campaign_id: int,
    model: str = Query(default=DEFAULT_MODEL),
    limit: int = Query(default=DEFAULT_ESTIMATE_LIMIT, ge=1, le=MAX_ESTIMATE_LIMIT),
    session: Session = Depends(get_session),
) -> GenerationCostOut:
    try:
        estimate = estimate_campaign_generation_cost(session, campaign_id, model=model, limit=limit)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except UnknownGenerationModel:
        raise HTTPException(
            status_code=400, detail=f"unknown model; choose one of {sorted(MODEL_LABELS)}"
        ) from None
    except GenerationCostConfigMissing:
        raise HTTPException(
            status_code=503, detail="no generation cost rate is configured for this model"
        ) from None
    return GenerationCostOut(
        campaign_id=estimate.campaign_id,
        model=estimate.model,
        config_version=estimate.config_version,
        rate_per_generation_usd=estimate.rate_per_generation_usd,
        rate_per_generation_kes=estimate.rate_per_generation_kes,
        step_count=estimate.step_count,
        enrolled_clients=estimate.enrolled_clients,
        estimated_templates=estimate.estimated_templates,
        single_generation=GenerationCostScenarioOut(
            count_per_step=estimate.single_generation.count_per_step,
            cost_per_step_usd=estimate.single_generation.cost_per_step_usd,
            cost_per_step_kes=estimate.single_generation.cost_per_step_kes,
            total_cost_usd=estimate.single_generation.total_cost_usd,
            total_cost_kes=estimate.single_generation.total_cost_kes,
        ),
        templates=GenerationCostScenarioOut(
            count_per_step=estimate.templates.count_per_step,
            cost_per_step_usd=estimate.templates.cost_per_step_usd,
            cost_per_step_kes=estimate.templates.cost_per_step_kes,
            total_cost_usd=estimate.templates.total_cost_usd,
            total_cost_kes=estimate.templates.total_cost_kes,
        ),
        as_of=estimate.as_of,
    )


@router.get("/{campaign_id}/readiness", response_model=CampaignReadinessOut)
def get_campaign_readiness(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignReadinessOut:
    try:
        counts = campaign_readiness(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignReadinessOut(campaign_id=campaign_id, **counts)


@router.get("/{campaign_id}/enrollments", response_model=Page[EnrollmentOut])
def get_campaign_enrollments(
    campaign_id: int,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[EnrollmentOut]:
    try:
        rows, next_cursor = list_campaign_enrollments(
            session, campaign_id, cursor=cursor, limit=limit
        )
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    items = [
        EnrollmentOut(
            enrollment_id=r.enrollment_id,
            campaign_id=r.campaign_id,
            client_id=r.client_id,
            status=r.status,
            current_step=r.current_step,
            next_due_at=r.next_due_at,
            priority_tier=r.priority_tier,
            message_angle=r.message_angle,
            value_band=r.value_band,
            recency_band=r.recency_band,
        )
        for r in rows
    ]
    return Page(items=items, next_cursor=next_cursor)


@router.get("/{campaign_id}/steps", response_model=list[CampaignStepOut])
def get_campaign_steps(
    campaign_id: int, session: Session = Depends(get_session)
) -> list[CampaignStepOut]:
    try:
        steps = list_campaign_steps(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return [
        CampaignStepOut(
            step_id=s.step_id,
            campaign_id=s.campaign_id,
            step_no=s.step_no,
            offset_days=s.offset_days,
            message_angle=s.message_angle,
            template_ref=s.template_ref,
        )
        for s in steps
    ]


@router.post("/{campaign_id}/steps", response_model=CampaignStepOut, status_code=201)
def post_campaign_step(
    campaign_id: int,
    body: CampaignStepCreateRequest,
    session: Session = Depends(get_session),
) -> CampaignStepOut:
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
    except NonIncreasingStepOffset as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
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
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=MAX_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> list[TouchOutcomeOut]:
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


@router.post("/{campaign_id}/send", response_model=list[TouchSendOutcomeOut])
def post_campaign_send(
    campaign_id: int, session: Session = Depends(get_session)
) -> list[TouchSendOutcomeOut]:
    try:
        outcomes = send_campaign(session, campaign_id)
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return [
        TouchSendOutcomeOut(
            touch_id=o.touch_id,
            enrollment_id=o.enrollment_id,
            sent=o.sent,
            delivery_status=o.delivery_status,
            reason=o.reason,
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
    background_tasks: BackgroundTasks,
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=MAX_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> GenerationBatchOut:
    settings = get_settings()
    try:
        batch = submit_campaign_batch(
            session,
            campaign_id,
            settings=settings,
            limit=limit,
            tracer=get_shared_tracer(),
        )
        session.commit()
    except CampaignNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="campaign not found") from None
    if batch.status == "submitted":
        background_tasks.add_task(
            poll_batch_until_done, batch.generation_batch_id, settings=settings
        )
    return _batch_out(batch)


@router.get("/{campaign_id}/batches/{generation_batch_id}", response_model=GenerationBatchOut)
def get_campaign_batch_status(
    campaign_id: int, generation_batch_id: str, session: Session = Depends(get_session)
) -> GenerationBatchOut:
    try:
        batch = get_campaign_batch(session, campaign_id, generation_batch_id)
    except BatchNotFound:
        raise HTTPException(status_code=404, detail="batch not found") from None
    return _batch_out(batch)


@router.get("/{campaign_id}/batches", response_model=Page[GenerationBatchOut])
def get_campaign_batches(
    campaign_id: int,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[GenerationBatchOut]:
    try:
        rows, next_cursor = list_campaign_batches(session, campaign_id, cursor=cursor, limit=limit)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[_batch_out(b) for b in rows], next_cursor=next_cursor)


@router.post(
    "/{campaign_id}/batches/{generation_batch_id}/ingest", response_model=BatchIngestResultOut
)
def post_campaign_batch_ingest(
    campaign_id: int, generation_batch_id: str, session: Session = Depends(get_session)
) -> BatchIngestResultOut:
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
    try:
        policy = get_campaign_template_policy(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return _policy_out(policy)


@router.put("/{campaign_id}/templates/policy", response_model=TemplatePolicyOut)
def put_campaign_templates_policy(
    campaign_id: int, body: TemplatePolicyRequest, session: Session = Depends(get_session)
) -> TemplatePolicyOut:
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
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=MAX_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> DraftTemplatesResult:
    try:
        outcome = draft_campaign_templates(
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
        estimated_templates=outcome.estimated_templates,
        effective_limit=outcome.effective_limit,
        drafted_count=outcome.drafted_count,
        skipped_existing=outcome.skipped_existing,
        failed_guardrails=outcome.failed_guardrails,
        failed_errors=outcome.failed_errors,
        policy=_policy_out(outcome.policy),
        templates=[MessageTemplateSummary.model_validate(t) for t in outcome.templates],
    )


@router.post(
    "/{campaign_id}/templates/{template_id}/instantiate",
    response_model=InstantiateTemplateResult,
)
def post_campaign_template_instantiate(
    campaign_id: int,
    template_id: str,
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=MAX_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> InstantiateTemplateResult:
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
