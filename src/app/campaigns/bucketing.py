"""Group a campaign's due, eligible clients by the facts that change what a
message may say, so one drafted template can serve everyone who shares them.

Read-only: this groups clients, it never drafts or touches anything.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.graph import ClientContext, ContextLoader, load_client_context
from app.campaigns.eligibility import check_eligibility
from app.campaigns.generation import resolve_product
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT, select_due_enrollments
from app.db.models.campaigns import Enrollment


@dataclass(frozen=True)
class ProfileKey:
    """The shared shape a client has to match to be filled from one template."""

    message_angle: str
    priority_tier: str | None
    product: str
    has_cadence: bool
    stale_contact: bool
    exit_reason_charge_settled: bool
    fund_name_known: bool

    def as_dict(self) -> dict[str, object]:
        """The JSONB shape message_template.profile_key stores."""
        return {
            "message_angle": self.message_angle,
            "priority_tier": self.priority_tier,
            "product": self.product,
            "has_cadence": self.has_cadence,
            "stale_contact": self.stale_contact,
            "exit_reason_charge_settled": self.exit_reason_charge_settled,
            "fund_name_known": self.fund_name_known,
        }


@dataclass(frozen=True)
class BucketMember:
    """One eligible client's enrollment, alongside the context that placed it."""

    enrollment: Enrollment
    context: ClientContext


@dataclass
class Bucket:
    """One profile's worth of due, eligible clients, ready for one draft."""

    profile_key: ProfileKey
    members: list[BucketMember] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)


def profile_key_sort_key(key: ProfileKey) -> tuple:
    """A stable, deterministic ordering over profile keys.

    Used as the final tie-break wherever buckets need a fixed order:
    listing an estimate's buckets, and deciding which buckets draft first
    when a template limit bites (campaigns.template_generation).
    """
    return (
        key.message_angle,
        key.priority_tier or "",
        key.product,
        key.has_cadence,
        key.stale_contact,
        key.exit_reason_charge_settled,
        key.fund_name_known,
    )


def profile_key_for(context: ClientContext, *, product: str) -> ProfileKey:
    """The bucket one client's own context belongs in."""
    facts = context.facts or {}
    return ProfileKey(
        message_angle=context.angle,
        priority_tier=context.priority_tier,
        product=product,
        has_cadence=bool(facts.get("invested_every_n_days")),
        stale_contact=bool(facts.get("stale_contact")),
        exit_reason_charge_settled=facts.get("exit_reason") == "charge_settled",
        fund_name_known=bool(facts.get("fund_name")),
    )


def derive_buckets(
    session: Session,
    campaign_id: int,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    context_loader: ContextLoader | None = None,
) -> list[Bucket]:
    """Group this campaign's due, eligible enrollments by shared profile.

    context_loader defaults to load_client_context bound to this session; a
    test passes a fake. A client it can't yet place is skipped.
    """
    context_loader = context_loader or functools.partial(load_client_context, session)

    due = select_due_enrollments(session, campaign_id=campaign_id, limit=limit)
    buckets: dict[ProfileKey, Bucket] = {}
    for enrollment in due:
        result = check_eligibility(session, enrollment)
        if not result.eligible:
            continue

        product = resolve_product(session, enrollment.client_id)
        try:
            context = context_loader(enrollment.client_id, product)
        except ValueError:
            continue

        key = profile_key_for(context, product=product)
        bucket = buckets.setdefault(key, Bucket(profile_key=key))
        bucket.members.append(BucketMember(enrollment=enrollment, context=context))

    return list(buckets.values())
