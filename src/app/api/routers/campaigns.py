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
    CampaignCreateOut,
    CampaignCreateRequest,
    CampaignDetailOut,
    CampaignListItemOut,
    CampaignReadinessOut,
    CampaignStepCreateRequest,
    CampaignStepOut,
    CampaignSummaryOut,
    EnrollmentOut,
    GenerationBatchOut,
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
    create_campaign,
    draft_campaign_templates,
    estimate_campaign_templates,
    get_campaign,
    get_campaign_batch,
    get_campaign_template_policy,
    ingest_campaign_batch,
    instantiate_campaign_template,
    list_campaign_enrollments,
    list_campaign_steps,
    list_campaigns,
    outreach_analytics,
    outreach_trend,
    run_campaign_generation,
    send_campaign,
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


@router.get("/analytics", response_model=OutreachAnalyticsOut)
def get_campaigns_analytics(session: Session = Depends(get_session)) -> OutreachAnalyticsOut:
    """Book-wide outreach analytics across every campaign: the enrollment
    funnel, cohort composition, drafting/review throughput, and how contact
    ends -- the dormant-outreach counterpart to GET /risk/analytics, an
    ops-facing read only.
    """
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
    """The last `days` calendar days' book-wide send and response activity,
    oldest first: the trend counterpart to the point-in-time /analytics
    snapshot above. An ops-facing read only.
    """
    points = outreach_trend(session, days=days)
    return OutreachTrendOut(
        points=[
            OutreachTrendPointOut(
                day=p.day, touches_sent=p.touches_sent, replies=p.replies, bounces=p.bounces
            )
            for p in points
        ]
    )


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign_detail(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignDetailOut:
    """One campaign's own fields, with no enrollment counts attached.

    Separate from GET /campaigns/{campaign_id}/summary, which is counts
    only, and from the list row, which a page landed on directly (a link,
    a reload) has no earlier fetch to scavenge fields from.
    """
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
    """Enrollment counts for one campaign, including rows suppressed as a duplicate person."""
    try:
        summary = campaign_summary(session, campaign_id)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return CampaignSummaryOut(campaign_id=campaign_id, **summary)


@router.get("/{campaign_id}/readiness", response_model=CampaignReadinessOut)
def get_campaign_readiness(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignReadinessOut:
    """Per-status counts for this campaign's templates and messages.

    Answers "is this campaign fully drafted and approved" in one read,
    instead of paging GET /reviews?campaign_id= and GET
    /templates?campaign_id= across every status and tallying client-side.
    """
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
    """One campaign's enrollment roster, oldest enrollment first.

    Distinct from GET /reviews?campaign_id=, which is message-level and
    only surfaces clients that already have a generated touch -- an
    enrolled client who hasn't been drafted for yet is invisible there but
    shows up here.
    """
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
    """A campaign's full send sequence, oldest step first.

    Without this, a caller has no way to see steps a previous session
    already persisted -- POST /{campaign_id}/steps only returns the one
    step it just appended.
    """
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
    """Append the next step in a campaign's send sequence.

    A campaign with no steps is enrolled but permanently idle: the
    eligibility gate refuses to generate a step that has no CampaignStep
    row, so this is required before /generate can do anything.

    offset_days must be strictly greater than the previous step's: the
    scheduler waits out the gap between two steps' offsets before the
    later one is due, so an equal or smaller offset makes it due
    immediately, the moment the step before it goes out.
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


@router.post("/{campaign_id}/send", response_model=list[TouchSendOutcomeOut])
def post_campaign_send(
    campaign_id: int, session: Session = Depends(get_session)
) -> list[TouchSendOutcomeOut]:
    """Send every approved, not-yet-sent touch in this campaign right now.

    Uses the stub sender until a real provider is wired in, so nothing is
    actually delivered yet, but the send gate, the audit trail, and each
    enrollment's advance to its next step all run for real. Flips the
    campaign to running the first time anything in this call sends.
    """
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
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=MAX_BATCH_LIMIT),
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
    limit: int = Query(default=DEFAULT_BATCH_LIMIT, ge=1, le=MAX_BATCH_LIMIT),
    session: Session = Depends(get_session),
) -> DraftTemplatesResult:
    """Group this campaign's due, eligible enrollments into buckets and draft
    one template per not-yet-templated bucket, up to the campaign's
    effective limit -- a third path alongside /generate and /generate/batch.
    Calling this again after raising the limit tops up rather than
    redrafting. Nothing is instantiated yet: each template needs its own
    review at GET/POST /templates first.
    """
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
