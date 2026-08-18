"""Campaign console reads and creation: enrollment counts, the campaign
table, turning a cohort filter into a real, enrolled campaign, defining its
send sequence, and running generation for whatever is due.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.agents.orchestrator import ChannelAgent
from app.audit.log import record_audit
from app.campaigns.batch_generation import BatchIngestResult, BatchNotFound
from app.campaigns.batch_generation import ingest_batch as ingest_campaign_batch_run
from app.campaigns.batch_generation import submit_batch as submit_campaign_batch_run
from app.campaigns.enrollment import enroll_cohort
from app.campaigns.estimation import TemplateEstimate, estimate_templates_sql
from app.campaigns.generation import generate_for_enrollment
from app.campaigns.instantiation import instantiate_template as instantiate_template_run
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT
from app.campaigns.template_generation import TemplateDraftOutcome, draft_templates_for_campaign
from app.campaigns.template_policy import (
    EffectivePolicy,
    get_effective_policy,
    set_campaign_policy,
)
from app.campaigns.touch import (
    SenderFn,
    SendOutcome,
    TouchRunOutcome,
    run_due_enrollments,
    stub_sender,
)
from app.campaigns.touch import send_due_touches as send_due_touches_run
from app.config import Settings
from app.db.models.campaigns import (
    CONTACT_EVENT_TYPES,
    CampaignStep,
    ContactEvent,
    Enrollment,
    TouchLog,
)
from app.db.models.generation_batch import GenerationBatch
from app.db.models.message_template import MessageTemplate
from app.db.models.models import ClientFeatures, Clients
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.rules import ClientMessageIndicators
from app.llmops.tracing import Tracer
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor
from app.privacy.llm_client import LLMClient
from app.services.clients import resolve_cohort_client_ids
from app.services.template_review import TemplateNotFound

REENGAGED_STATUS = "stopped_reengaged"
ACTIVE_CAMPAIGN_STATUSES = ("draft", "running")


class CampaignNotFound(Exception):
    """No campaign exists with the given id."""


class NonIncreasingStepOffset(Exception):
    """A new step's offset_days does not come after the previous step's.

    The scheduler measures the wait before a step from the gap between its
    offset and the one before it (see advance_enrollment). An offset that
    does not increase collapses that gap to zero or less, so the new step
    goes out the moment the previous one does instead of waiting.
    """

    def __init__(self, offset_days: int, previous_offset_days: int) -> None:
        self.offset_days = offset_days
        self.previous_offset_days = previous_offset_days
        super().__init__(
            f"offset_days {offset_days} must be greater than the previous "
            f"step's offset_days {previous_offset_days}"
        )


def get_campaign(session: Session, campaign_id: int) -> Campaign:
    """One campaign's own row. Raises CampaignNotFound the same way every
    other campaign-scoped call does.
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(campaign_id)
    return campaign


def list_campaign_enrollments(
    session: Session,
    campaign_id: int,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Row], str | None]:
    """One campaign's enrollment roster, oldest enrollment first.

    Distinct from the review queue: an enrolled client with no generated
    touch yet still shows up here. priority_tier/message_angle/value_band/
    recency_band are joined in from the same model-safe bucket tables the
    client console reads, cheaply, so the roster isn't bare ids.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    limit = clamp_limit(limit)
    query = (
        select(
            Enrollment.enrollment_id,
            Enrollment.campaign_id,
            Enrollment.client_id,
            Enrollment.status,
            Enrollment.current_step,
            Enrollment.next_due_at,
            ClientMessageIndicators.priority_tier,
            ClientMessageIndicators.message_angle,
            ClientFeatures.value_band,
            ClientFeatures.recency_band,
        )
        .select_from(Enrollment)
        .join(ClientFeatures, ClientFeatures.client_id == Enrollment.client_id, isouter=True)
        .join(
            ClientMessageIndicators,
            ClientMessageIndicators.client_id == Enrollment.client_id,
            isouter=True,
        )
        .where(Enrollment.campaign_id == campaign_id)
    )
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(Enrollment.enrollment_id > after_id)
    query = query.order_by(Enrollment.enrollment_id).limit(limit + 1)

    rows = list(session.execute(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].enrollment_id)
    return rows, next_cursor


def campaign_summary(session: Session, campaign_id: int) -> dict[str, int]:
    """Enrollment counts for one campaign: total, primary, and suppressed rows.

    A suppressed row is enrolled but never sends: is_primary_contact_row is
    false because another client_id for the same person already claimed it.
    Raises CampaignNotFound when campaign_id names no campaign.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    total = session.execute(
        select(func.count()).select_from(Enrollment).where(Enrollment.campaign_id == campaign_id)
    ).scalar_one()
    primary = session.execute(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.campaign_id == campaign_id,
            Enrollment.is_primary_contact_row.is_(True),
        )
    ).scalar_one()

    return {
        "total_enrolled": total,
        "primary_count": primary,
        "suppressed_count": total - primary,
    }


def campaign_value(session: Session, campaign_id: int) -> dict[str, float | int]:
    """What one campaign's cohort was worth, for ROI reporting.

    Sums total_purchase_amount (KES, the client's own historical buying,
    not their current balance) across primary enrollment rows only, the
    same "one person counts once" scope campaign_summary's primary_count
    and outreach_analytics use. A suppressed row is a duplicate person, not
    a second member of the cohort, so summing it too would double-count
    that person's value.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    row = session.execute(
        select(
            func.count(Clients.client_id),
            func.coalesce(func.sum(Clients.total_purchase_amount), 0.0),
        )
        .select_from(Enrollment)
        .join(Clients, Clients.client_id == Enrollment.client_id)
        .where(
            Enrollment.campaign_id == campaign_id,
            Enrollment.is_primary_contact_row.is_(True),
        )
    ).one()
    valued_count, estimated_value = row

    return {
        "valued_count": valued_count,
        "estimated_value": float(estimated_value),
    }


def campaign_readiness(session: Session, campaign_id: int) -> dict[str, dict[str, int]]:
    """Per-status counts for one campaign's templates and messages.

    Answers "is this campaign fully drafted and approved" in one read,
    instead of paging GET /reviews and GET /templates across every status
    and tallying client-side. A status with no rows is left out of its
    dict rather than reported as zero.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    template_rows = session.execute(
        select(MessageTemplate.status, func.count())
        .where(MessageTemplate.campaign_id == campaign_id)
        .group_by(MessageTemplate.status)
    ).all()
    message_rows = session.execute(
        select(OutreachMessage.status, func.count())
        .where(OutreachMessage.campaign_id == campaign_id)
        .group_by(OutreachMessage.status)
    ).all()

    return {
        "templates": {status: count for status, count in template_rows},
        "messages": {status: count for status, count in message_rows},
    }


def list_campaigns(
    session: Session,
    *,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Row], str | None]:
    """Campaigns oldest-first, each carrying its own enrollment counts.

    One join instead of one summary query per row, since a real campaign
    table needs both the campaign's own fields and its counts at once.
    """
    limit = clamp_limit(limit)
    is_primary = Enrollment.is_primary_contact_row.is_(True)
    query = (
        select(
            Campaign.campaign_id,
            Campaign.name,
            Campaign.campaign_type,
            Campaign.status,
            Campaign.cohort_definition,
            Campaign.start_date,
            Campaign.end_date,
            Campaign.created_at,
            func.count(Enrollment.enrollment_id).label("total_enrolled"),
            func.count(Enrollment.enrollment_id).filter(is_primary).label("primary_count"),
        )
        .select_from(Campaign)
        .join(Enrollment, Enrollment.campaign_id == Campaign.campaign_id, isouter=True)
        .group_by(Campaign.campaign_id)
    )
    if status is not None:
        query = query.where(Campaign.status == status)
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(Campaign.campaign_id > after_id)
    query = query.order_by(Campaign.campaign_id).limit(limit + 1)

    rows = list(session.execute(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].campaign_id)
    return rows, next_cursor


def create_campaign(
    session: Session,
    *,
    name: str,
    campaign_type: str,
    cohort_filters: dict,
    start_date=None,
    end_date=None,
) -> tuple[Campaign, int]:
    """Create a campaign and enroll every client currently matching its cohort.

    cohort_filters is stored as-is on cohort_definition, the allow-listed
    feature values the cohort was selected on, so membership can be
    re-derived later; it is also used once, right here, to resolve the
    client_ids enroll_cohort actually enrolls. Returns the new campaign and
    how many client_ids matched (not how many are primary — see
    campaign_summary for the primary/suppressed split).
    """
    campaign = Campaign(
        name=name,
        campaign_type=campaign_type,
        cohort_definition=cohort_filters,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(campaign)
    session.flush()

    client_ids = resolve_cohort_client_ids(session, **cohort_filters)
    enroll_cohort(session, campaign_id=campaign.campaign_id, client_ids=client_ids)

    record_audit(
        session,
        entity_type="campaign",
        action="create",
        entity_id=str(campaign.campaign_id),
        detail={"cohort_filters": cohort_filters, "matched_count": len(client_ids)},
    )
    session.flush()
    return campaign, len(client_ids)


def add_campaign_step(
    session: Session,
    campaign_id: int,
    *,
    offset_days: int,
    message_angle: str,
    template_ref: str | None = None,
) -> CampaignStep:
    """Append the next step in a campaign's send sequence.

    step_no is assigned as one past whatever already exists, so building a
    sequence is just calling this once per step in order; the eligibility
    gate refuses to generate a step for which no CampaignStep row exists
    yet, so a campaign with none is enrolled but permanently idle.

    offset_days must be strictly greater than the previous step's, so the
    scheduler always has a positive gap to wait out before the new step is
    due; see NonIncreasingStepOffset.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    previous_step = session.execute(
        select(CampaignStep)
        .where(CampaignStep.campaign_id == campaign_id)
        .order_by(CampaignStep.step_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous_step is not None and offset_days <= previous_step.offset_days:
        raise NonIncreasingStepOffset(offset_days, previous_step.offset_days)

    next_step_no = (previous_step.step_no if previous_step is not None else 0) + 1

    step = CampaignStep(
        campaign_id=campaign_id,
        step_no=next_step_no,
        offset_days=offset_days,
        message_angle=message_angle,
        template_ref=template_ref,
    )
    session.add(step)
    session.flush()
    record_audit(
        session,
        entity_type="campaign_step",
        action="create",
        entity_id=str(step.step_id),
        detail={"campaign_id": campaign_id, "step_no": next_step_no, "offset_days": offset_days},
    )
    return step


def list_campaign_steps(session: Session, campaign_id: int) -> list[CampaignStep]:
    """A campaign's full send sequence, oldest step first.

    This is the read side of add_campaign_step: without it a caller has no
    way to see steps a previous session already persisted, only whatever
    it appends itself.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    return list(
        session.execute(
            select(CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.step_no)
        ).scalars()
    )


def run_campaign_generation(
    session: Session,
    campaign_id: int,
    *,
    agent: ChannelAgent,
    settings: Settings,
    tracer: Tracer | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> list[TouchRunOutcome]:
    """Generate a touch for every one of this campaign's due, eligible
    enrollments, using the given channel agent for the actual drafting.

    A thin binding over run_due_enrollments: this module owns nothing about
    generation itself, only wires generate_for_enrollment's extra
    (agent, settings, tracer) arguments in as the GenerateFn campaigns.touch
    expects.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)

    generate = functools.partial(
        generate_for_enrollment, agent=agent, settings=settings, tracer=tracer
    )
    return run_due_enrollments(session, campaign_id=campaign_id, generate=generate, limit=limit)


def send_campaign(
    session: Session, campaign_id: int, *, sender: SenderFn = stub_sender
) -> list[SendOutcome]:
    """Send every approved, not-yet-sent touch in this campaign right now.

    Flips the campaign's own status from draft to running the first time
    anything in this call actually sends -- a no-op once it already has a
    later status. Raises CampaignNotFound the same way the other
    campaign-scoped calls do.
    """
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(campaign_id)

    outcomes = send_due_touches_run(session, campaign_id=campaign_id, sender=sender)
    if campaign.status == "draft" and any(o.sent for o in outcomes):
        campaign.status = "running"
        session.flush()
        record_audit(
            session,
            entity_type="campaign",
            action="status_change",
            entity_id=str(campaign_id),
            detail={"from": "draft", "to": "running"},
        )
    return outcomes


def submit_campaign_batch(
    session: Session,
    campaign_id: int,
    *,
    settings: Settings,
    limit: int = DEFAULT_BATCH_LIMIT,
    tracer: Tracer | None = None,
) -> GenerationBatch:
    """Submit this campaign's due, eligible enrollments to the model
    provider's batch endpoint in one call, an alternative to
    run_campaign_generation for a cohort too large to draft one request at
    a time. Raises CampaignNotFound the same way run_campaign_generation
    does; campaigns.batch_generation.submit_batch does the rest.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)
    return submit_campaign_batch_run(
        session, campaign_id, settings=settings, limit=limit, tracer=tracer
    )


def ingest_campaign_batch(
    session: Session,
    campaign_id: int,
    generation_batch_id: str,
    *,
    settings: Settings,
    tracer: Tracer | None = None,
) -> BatchIngestResult:
    """Turn a submitted batch's results into pending-review messages, once
    the provider reports it has ended. Raises BatchNotFound both when no
    such batch exists and when it exists under a different campaign, since
    either way it names nothing this campaign's endpoint can act on -- checked
    before any ingestion work runs, not after.
    """
    batch = session.get(GenerationBatch, generation_batch_id)
    if batch is None or batch.campaign_id != campaign_id:
        raise BatchNotFound(generation_batch_id)
    return ingest_campaign_batch_run(session, generation_batch_id, settings=settings, tracer=tracer)


def get_campaign_batch(
    session: Session, campaign_id: int, generation_batch_id: str
) -> GenerationBatch:
    """One batch submission, scoped to the campaign it was submitted under."""
    batch = session.get(GenerationBatch, generation_batch_id)
    if batch is None or batch.campaign_id != campaign_id:
        raise BatchNotFound(generation_batch_id)
    return batch


def draft_campaign_templates(
    session: Session,
    campaign_id: int,
    *,
    settings: Settings,
    llm_client: LLMClient,
    limit: int = DEFAULT_BATCH_LIMIT,
    tracer: Tracer | None = None,
) -> TemplateDraftOutcome:
    """Derive this campaign's buckets and draft one template for each due,
    not-yet-templated bucket, up to the campaign's effective limit -- a
    third path alongside run_campaign_generation and submit_campaign_batch.
    Raises CampaignNotFound the same way the other two do.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)
    return draft_templates_for_campaign(
        session, campaign_id, settings=settings, llm_client=llm_client, limit=limit, tracer=tracer
    )


def instantiate_campaign_template(
    session: Session,
    campaign_id: int,
    template_id: str,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> list[OutreachMessage]:
    """Instantiate every due, eligible client currently matching an
    approved template's profile. Raises CampaignNotFound / TemplateNotFound
    the same ownership-scoped way get_campaign_batch does for a batch.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)
    template = session.get(MessageTemplate, template_id)
    if template is None or template.campaign_id != campaign_id:
        raise TemplateNotFound(template_id)
    return instantiate_template_run(session, template, campaign_id=campaign_id, limit=limit)


def estimate_campaign_templates(
    session: Session, campaign_id: int, *, limit: int = DEFAULT_BATCH_LIMIT
) -> TemplateEstimate:
    """How many templates drafting this campaign right now would produce.

    Read-only and never constructs an LLMClient: estimate_templates_sql
    resolves the due, eligible cohort from bulk column reads, not a
    ClientContext per client. Raises CampaignNotFound the same way the
    other campaign-scoped calls do.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)
    return estimate_templates_sql(session, campaign_id, limit=limit)


def get_campaign_template_policy(session: Session, campaign_id: int) -> EffectivePolicy:
    """The limit in force for this campaign: its own override if it has set
    one, otherwise the active system default. Raises CampaignNotFound the
    same way the other campaign-scoped calls do.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)
    return get_effective_policy(session, campaign_id)


def set_campaign_template_policy(
    session: Session,
    campaign_id: int,
    *,
    max_templates: int | None,
    max_templates_pct: int | None,
    updated_by: str,
) -> EffectivePolicy:
    """Set this campaign's own template generation limit, overriding the
    system default. Raises CampaignNotFound the same way the other
    campaign-scoped calls do.
    """
    if session.get(Campaign, campaign_id) is None:
        raise CampaignNotFound(campaign_id)
    policy = set_campaign_policy(
        session,
        campaign_id,
        max_templates=max_templates,
        max_templates_pct=max_templates_pct,
        updated_by=updated_by,
    )
    return EffectivePolicy(
        source="campaign",
        max_templates=policy.max_templates,
        max_templates_pct=policy.max_templates_pct,
        updated_at=policy.updated_at,
        updated_by=policy.updated_by,
    )


@dataclass(frozen=True)
class OutreachAnalytics:
    """Book-wide outreach analytics across every campaign: the enrollment
    funnel, cohort composition, drafting/review throughput, and how contact
    ends -- the candidate cuts a dormant-outreach analytics page needs, the
    outreach counterpart of services/risk.py's own RiskAnalytics.
    """

    total_enrolled: int
    primary_count: int
    suppressed_count: int
    active_campaign_count: int
    by_enrollment_status: list[tuple[str, int]]
    by_value_band: list[tuple[str, int]]
    by_recency_band: list[tuple[str, int]]
    by_priority_tier: list[tuple[str | None, int]]
    by_message_angle: list[tuple[str | None, int]]
    by_message_status: list[tuple[str, int]]
    by_review_outcome: list[tuple[str, int]]
    by_contact_event: list[tuple[str, int]]
    reengaged_count: int
    reengagement_rate: float


def outreach_analytics(session: Session) -> OutreachAnalytics:
    """Book-wide outreach analytics, read across every campaign at once.

    Enrollment status, message status, and review outcome are counted over
    every row that exists, the same "whole current population" scope
    risk_analytics uses. The cohort cuts (value_band, recency_band,
    priority_tier, message_angle) are scoped to primary enrollment rows
    only, joined to the same model-safe bucket tables the enrollment
    roster reads -- a suppressed row is a duplicate person, not a second
    member of the cohort, so counting it too would double-count that
    person's bucket.
    """

    total_enrolled = session.scalar(select(func.count()).select_from(Enrollment)) or 0
    primary_count = (
        session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(Enrollment.is_primary_contact_row.is_(True))
        )
        or 0
    )

    active_campaign_count = (
        session.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.status.in_(ACTIVE_CAMPAIGN_STATUSES))
        )
        or 0
    )

    by_enrollment_status = list(
        session.execute(
            select(Enrollment.status, func.count())
            .select_from(Enrollment)
            .group_by(Enrollment.status)
            .order_by(func.count().desc())
        ).all()
    )

    primary_cohort = (
        select(Enrollment.client_id)
        .where(Enrollment.is_primary_contact_row.is_(True))
        .distinct()
        .subquery()
    )
    by_value_band = list(
        session.execute(
            select(ClientFeatures.value_band, func.count())
            .select_from(primary_cohort)
            .join(ClientFeatures, ClientFeatures.client_id == primary_cohort.c.client_id)
            .group_by(ClientFeatures.value_band)
            .order_by(func.count().desc())
        ).all()
    )
    by_recency_band = list(
        session.execute(
            select(ClientFeatures.recency_band, func.count())
            .select_from(primary_cohort)
            .join(ClientFeatures, ClientFeatures.client_id == primary_cohort.c.client_id)
            .group_by(ClientFeatures.recency_band)
            .order_by(func.count().desc())
        ).all()
    )
    by_priority_tier = list(
        session.execute(
            select(ClientMessageIndicators.priority_tier, func.count())
            .select_from(primary_cohort)
            .join(
                ClientMessageIndicators,
                ClientMessageIndicators.client_id == primary_cohort.c.client_id,
                isouter=True,
            )
            .group_by(ClientMessageIndicators.priority_tier)
            .order_by(func.count().desc())
        ).all()
    )
    by_message_angle = list(
        session.execute(
            select(ClientMessageIndicators.message_angle, func.count())
            .select_from(primary_cohort)
            .join(
                ClientMessageIndicators,
                ClientMessageIndicators.client_id == primary_cohort.c.client_id,
                isouter=True,
            )
            .group_by(ClientMessageIndicators.message_angle)
            .order_by(func.count().desc())
        ).all()
    )

    by_message_status = list(
        session.execute(
            select(OutreachMessage.status, func.count())
            .select_from(OutreachMessage)
            .group_by(OutreachMessage.status)
            .order_by(func.count().desc())
        ).all()
    )
    by_review_outcome = list(
        session.execute(
            select(ReviewAction.outcome, func.count())
            .select_from(ReviewAction)
            .group_by(ReviewAction.outcome)
            .order_by(func.count().desc())
        ).all()
    )

    event_counts = dict(
        session.execute(
            select(ContactEvent.type, func.count())
            .select_from(ContactEvent)
            .group_by(ContactEvent.type)
        ).all()
    )
    by_contact_event = [(t, event_counts.get(t, 0)) for t in CONTACT_EVENT_TYPES]

    reengaged_count = (
        session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(
                Enrollment.is_primary_contact_row.is_(True),
                Enrollment.status == REENGAGED_STATUS,
            )
        )
        or 0
    )
    reengagement_rate = reengaged_count / primary_count if primary_count else 0.0

    return OutreachAnalytics(
        total_enrolled=total_enrolled,
        primary_count=primary_count,
        suppressed_count=total_enrolled - primary_count,
        active_campaign_count=active_campaign_count,
        by_enrollment_status=by_enrollment_status,
        by_value_band=by_value_band,
        by_recency_band=by_recency_band,
        by_priority_tier=by_priority_tier,
        by_message_angle=by_message_angle,
        by_message_status=by_message_status,
        by_review_outcome=by_review_outcome,
        by_contact_event=by_contact_event,
        reengaged_count=reengaged_count,
        reengagement_rate=reengagement_rate,
    )


@dataclass(frozen=True)
class OutreachTrendPoint:
    """One calendar day's book-wide send and response activity."""

    day: date
    touches_sent: int
    replies: int
    bounces: int


def outreach_trend(session: Session, *, days: int = 30) -> list[OutreachTrendPoint]:
    """The last `days` calendar days' book-wide send and response activity,
    oldest first, for trend charts. Every day in the window is present, even
    a day with no activity at all, unlike risk_trend which only has a point
    per completed nightly run -- outreach has no equivalent run cadence, so
    the day itself is the unit.
    """
    days = max(1, min(days, 90))
    end = date.today()
    start = end - timedelta(days=days - 1)

    sent_by_day = dict(
        session.execute(
            select(func.date(TouchLog.sent_at), func.count())
            .where(TouchLog.sent_at.isnot(None), func.date(TouchLog.sent_at) >= start)
            .group_by(func.date(TouchLog.sent_at))
        ).all()
    )

    events_by_day: dict[date, dict[str, int]] = defaultdict(dict)
    for day, event_type, count in session.execute(
        select(func.date(ContactEvent.occurred_at), ContactEvent.type, func.count())
        .where(ContactEvent.occurred_at >= start)
        .group_by(func.date(ContactEvent.occurred_at), ContactEvent.type)
    ).all():
        events_by_day[day][event_type] = count

    points: list[OutreachTrendPoint] = []
    day = start
    while day <= end:
        points.append(
            OutreachTrendPoint(
                day=day,
                touches_sent=sent_by_day.get(day, 0),
                replies=events_by_day.get(day, {}).get("reply", 0),
                bounces=events_by_day.get(day, {}).get("bounce", 0),
            )
        )
        day += timedelta(days=1)
    return points
