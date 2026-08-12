"""How many distinct templates a campaign's current configuration would
produce, without drafting anything and without loading a single
ClientContext.

Two estimators, and they must agree exactly on the same cohort:

- estimate_templates_reference calls derive_buckets, the same walk
  generation itself does. It cannot drift from what generation produces,
  because it is the same function -- but it pays for a ClientContext per
  client (a RAG lookup among them), which is fine at a few thousand rows and
  not something a campaign manager clicks twice at fifty thousand.
- estimate_templates_sql reads the same columns in bulk instead: the due
  cohort comes from the same select_due_enrollments query derive_buckets
  itself calls, then eligibility and the profile key are resolved from a
  handful of batched reads instead of one round trip per client. Its
  correctness is only as good as its test against the reference
  (tests/test_campaigns_estimation.py); if that test ever fails, this
  function is wrong, not the test.

Neither estimator constructs an LLMClient, drafts anything, or persists an
enrollment state change. The one real write either can cause is the audited
pii_vault read the eligibility gate already performs for the opt-out and
contact checks: the reference makes one such read per candidate client (it
is calling the real gate), the SQL path makes one batched read for however
many candidates are still in play by the time it needs vault data. Both are
committed through restricted_session regardless of whether the caller
commits its own session, same as the eligibility gate today; this module
does not change that, only how many rows one estimate produces.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph import ContextLoader
from app.audit.log import record_audit
from app.campaigns.bucketing import Bucket, ProfileKey, derive_buckets
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT, select_due_enrollments
from app.config import get_settings
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.models import ClientFeatures, ClientFund, Clients, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import restricted_session
from app.privacy.fact_block import FUND_DISPLAY_NAMES

_UNRESOLVED_MESSAGE_STATUSES = ("pending_review", "escalated", "held")
_STOPPING_EVENT_TYPES = ("bounce", "complaint")

# An estimate is read-only and cheap relative to drafting, so its own
# default is well above DEFAULT_BATCH_LIMIT: the point of the fast path is
# answering "the whole due book" affordably, not just the next batch of it.
DEFAULT_ESTIMATE_LIMIT = 5000
MAX_ESTIMATE_LIMIT = 100_000


@dataclass(frozen=True)
class EstimatedBucket:
    """One profile's worth of due, eligible clients, and nothing else."""

    profile_key: ProfileKey
    client_count: int


@dataclass(frozen=True)
class TemplateEstimate:
    """The three-field answer to "how many templates would this produce".

    limit and as_of are computed_from: returning the inputs is what makes
    "same configuration, same number" a checkable claim, since the due
    cohort moves as time passes and as touches are recorded.
    """

    estimated_templates: int
    eligible_clients: int
    buckets: tuple[EstimatedBucket, ...]
    limit: int
    as_of: datetime


def _profile_key_sort_key(key: ProfileKey) -> tuple:
    return (
        key.message_angle,
        key.priority_tier or "",
        key.product,
        key.has_cadence,
        key.stale_contact,
        key.exit_reason_charge_settled,
        key.fund_name_known,
    )


def _to_estimate(buckets: Sequence[EstimatedBucket], *, limit: int) -> TemplateEstimate:
    counted = sorted(buckets, key=lambda b: _profile_key_sort_key(b.profile_key))
    return TemplateEstimate(
        estimated_templates=len(counted),
        eligible_clients=sum(b.client_count for b in counted),
        buckets=tuple(counted),
        limit=limit,
        as_of=datetime.now(UTC),
    )


def _count_buckets(buckets: Sequence[Bucket]) -> list[EstimatedBucket]:
    return [EstimatedBucket(profile_key=b.profile_key, client_count=b.size) for b in buckets]


def estimate_templates_reference(
    session: Session,
    campaign_id: int,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    context_loader: ContextLoader | None = None,
) -> TemplateEstimate:
    """The estimate derive_buckets itself produces: correct by construction,
    and the reference the fast path is held to.
    """
    buckets = derive_buckets(session, campaign_id, limit=limit, context_loader=context_loader)
    return _to_estimate(_count_buckets(buckets), limit=limit)


# ---------------------------------------------------------------------------
# The fast path: bulk reads instead of one ClientContext per client.
# ---------------------------------------------------------------------------


def estimate_templates_sql(
    session: Session, campaign_id: int, *, limit: int = DEFAULT_BATCH_LIMIT
) -> TemplateEstimate:
    """The same estimate as estimate_templates_reference, computed from a
    handful of batched column reads instead of one ClientContext per client.
    """
    due = select_due_enrollments(session, campaign_id=campaign_id, limit=limit)
    if not due:
        return _to_estimate([], limit=limit)

    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.status in ("paused", "completed"):
        return _to_estimate([], limit=limit)

    profile_keys = _resolve_eligible_profile_keys(session, due, campaign_id=campaign_id)
    counts: dict[ProfileKey, int] = {}
    for key in profile_keys:
        counts[key] = counts.get(key, 0) + 1
    buckets = [EstimatedBucket(profile_key=k, client_count=v) for k, v in counts.items()]
    return _to_estimate(buckets, limit=limit)


def _resolve_eligible_profile_keys(
    session: Session, due: Sequence[Enrollment], *, campaign_id: int
) -> list[ProfileKey]:
    """One ProfileKey per due enrollment that survives the eligibility gate
    and has enough data to be placed. Mirrors check_eligibility and
    profile_key_for condition for condition; see the module docstring.
    """
    client_ids = [e.client_id for e in due]
    enrollment_ids = [e.enrollment_id for e in due]

    existing_steps = set(
        session.scalars(select(CampaignStep.step_no).where(CampaignStep.campaign_id == campaign_id))
    )
    features_by_client = {
        row.client_id: row
        for row in session.scalars(
            select(ClientFeatures).where(ClientFeatures.client_id.in_(client_ids))
        )
    }
    indicators_by_client = {
        row.client_id: row
        for row in session.scalars(
            select(ClientMessageIndicators).where(ClientMessageIndicators.client_id.in_(client_ids))
        )
    }
    touches_by_enrollment: dict[int, list[TouchLog]] = {}
    touch_query = select(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids))
    for touch in session.scalars(touch_query):
        touches_by_enrollment.setdefault(touch.enrollment_id, []).append(touch)
    message_ids = {
        t.message_id
        for touches in touches_by_enrollment.values()
        for t in touches
        if t.message_id is not None
    }
    messages_by_id = (
        {
            row.message_id: row
            for row in session.scalars(
                select(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
        }
        if message_ids
        else {}
    )
    suppressed_client_ids = set(
        session.scalars(select(Suppression.client_id).where(Suppression.client_id.in_(client_ids)))
    )

    # Phase one: every condition that needs no PII vault read. Candidates
    # still standing after this are the only clients whose vault booleans
    # are worth reading at all.
    candidates: list[Enrollment] = []
    for enrollment in due:
        step_no = enrollment.current_step + 1
        if step_no not in existing_steps:
            continue
        features = features_by_client.get(enrollment.client_id)
        if features is None or features.purchase_depth == "none":
            continue
        if enrollment.client_id not in indicators_by_client:
            continue
        touches = touches_by_enrollment.get(enrollment.enrollment_id, ())
        if any(t.step_no == step_no for t in touches):
            continue
        if _has_unresolved_touch(touches, messages_by_id):
            continue
        if enrollment.client_id in suppressed_client_ids:
            continue
        candidates.append(enrollment)

    if not candidates:
        return []

    candidate_client_ids = list({e.client_id for e in candidates})
    settings = get_settings()
    vault_signals = _bulk_vault_signals(candidate_client_ids)
    stopping_client_ids, reply_events_by_client = _bulk_contact_events(
        session, candidate_client_ids
    )
    clients_by_id = {
        row.client_id: row
        for row in session.scalars(
            select(Clients).where(Clients.client_id.in_(candidate_client_ids))
        )
    }
    cooldown_client_ids = _bulk_cooldown(
        session, candidate_client_ids, settings.campaign_cooldown_days
    )
    primary_fund_by_client = {
        row.client_id: row
        for row in session.scalars(
            select(ClientFund).where(
                ClientFund.client_id.in_(candidate_client_ids),
                ClientFund.is_primary_contact_row.is_(True),
            )
        )
    }

    resolved: list[ProfileKey] = []
    for enrollment in candidates:
        client_id = enrollment.client_id
        opted_out, has_contact = vault_signals.get(client_id, (False, False))
        if opted_out:
            continue
        if not has_contact and settings.require_deliverable_contact:
            continue
        if client_id in stopping_client_ids:
            continue

        touches = touches_by_enrollment.get(enrollment.enrollment_id, ())
        last_sent_at = _last_touch_sent_at(touches, enrollment)
        reply_times = reply_events_by_client.get(client_id, ())
        if any(occurred_at > last_sent_at for occurred_at in reply_times):
            continue

        if _reengaged(clients_by_id.get(client_id), enrollment):
            continue
        if client_id in cooldown_client_ids:
            continue

        features = features_by_client[client_id]
        indicator = indicators_by_client[client_id]
        primary = primary_fund_by_client.get(client_id)
        resolved.append(_profile_key_from_columns(features, indicator, primary))

    return resolved


def _profile_key_from_columns(
    features: ClientFeatures,
    indicator: ClientMessageIndicators,
    primary_fund: ClientFund | None,
) -> ProfileKey:
    """Same fields load_client_facts + profile_key_for would produce, read
    straight off the columns behind them. primary_fund missing means the
    fact block itself would have been None, which zeroes every fact-derived
    field below, not just the cadence one.
    """
    has_facts = primary_fund is not None
    has_cadence = (
        has_facts
        and primary_fund.rhythm_days is not None
        and primary_fund.rhythm_days >= 1
        and features.cadence_band not in (None, "None")
    )
    return ProfileKey(
        message_angle=indicator.message_angle,
        priority_tier=indicator.priority_tier,
        product=(features.fund_type or "other").replace("_", " "),
        has_cadence=bool(has_cadence),
        stale_contact=has_facts and bool(features.stale_contact),
        exit_reason_charge_settled=has_facts and features.exit_reason == "charge_settled",
        fund_name_known=has_facts and features.fund_type in FUND_DISPLAY_NAMES,
    )


def _has_unresolved_touch(
    touches: Sequence[TouchLog], messages_by_id: dict[str, OutreachMessage]
) -> bool:
    if not touches:
        return False
    latest = max(touches, key=lambda t: t.step_no)
    if latest.message_id is None:
        return True
    message = messages_by_id.get(latest.message_id)
    if message is None:
        return True
    if message.status in _UNRESOLVED_MESSAGE_STATUSES:
        return True
    return message.status == "approved" and latest.sent_at is None


def _last_touch_sent_at(touches: Sequence[TouchLog], enrollment: Enrollment) -> datetime:
    sent_ats = [t.sent_at for t in touches if t.sent_at is not None]
    return max(sent_ats) if sent_ats else enrollment.enrolled_at


def _reengaged(client: Clients | None, enrollment: Enrollment) -> bool:
    if client is None:
        return False
    if client.balance is not None and client.balance > 0:
        return True
    return bool(
        client.last_activity_date is not None
        and client.last_activity_date > enrollment.enrolled_at.date()
    )


def _bulk_contact_events(
    session: Session, client_ids: Sequence[int]
) -> tuple[set[int], dict[int, list[datetime]]]:
    """(client_ids with a bounce/complaint ever, client_id -> reply timestamps)."""
    if not client_ids:
        return set(), {}
    rows = session.execute(
        select(ContactEvent.client_id, ContactEvent.type, ContactEvent.occurred_at).where(
            ContactEvent.client_id.in_(client_ids),
            ContactEvent.type.in_((*_STOPPING_EVENT_TYPES, "reply")),
        )
    ).all()
    stopping = {row.client_id for row in rows if row.type in _STOPPING_EVENT_TYPES}
    replies: dict[int, list[datetime]] = {}
    for row in rows:
        if row.type == "reply":
            replies.setdefault(row.client_id, []).append(row.occurred_at)
    return stopping, replies


def _bulk_cooldown(session: Session, client_ids: Sequence[int], cooldown_days: int) -> set[int]:
    """client_ids with a touch sent, on any campaign, within the cooldown window."""
    if not client_ids:
        return set()
    cutoff = func.now() - timedelta(days=cooldown_days)
    rows = session.execute(
        select(Enrollment.client_id)
        .join(TouchLog, TouchLog.enrollment_id == Enrollment.enrollment_id)
        .where(
            Enrollment.client_id.in_(client_ids),
            TouchLog.sent_at.isnot(None),
            TouchLog.sent_at >= cutoff,
        )
        .distinct()
    ).scalars()
    return set(rows)


def _bulk_vault_signals(client_ids: Sequence[int]) -> dict[int, tuple[bool, bool]]:
    """client_id -> (opted_out, has_deliverable_contact), one audited batch read."""
    if not client_ids:
        return {}
    with restricted_session() as session:
        rows = session.execute(
            select(
                PiiVault.client_id,
                PiiVault.opt_out_flag,
                PiiVault.contact_email,
                PiiVault.contact_whatsapp,
            ).where(PiiVault.client_id.in_(client_ids))
        ).all()
        record_audit(
            session,
            entity_type="pii_vault",
            action="read_batch",
            detail={"count": len(client_ids), "purpose": "template_estimate"},
        )
        session.commit()
    return {
        row.client_id: (row.opt_out_flag, bool(row.contact_email or row.contact_whatsapp))
        for row in rows
    }
