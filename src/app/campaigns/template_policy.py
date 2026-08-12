"""How many templates one drafting call may produce.

Two sources, one function to reconcile them: a campaign's own
campaign_template_policy row if it has set one, else whichever
template_policy_config_version is active. effective_limit is the one place
that turns "absolute cap" and "percentage of the estimate" into a single
number, so no caller invents its own rule for combining them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.template_policy import CampaignTemplatePolicy, TemplatePolicyConfigVersion


class TemplatePolicyValidationError(ValueError):
    """A template policy or default config failed validation and was not written."""


def _validate_limits(max_templates: int | None, max_templates_pct: int | None) -> None:
    if max_templates is not None and max_templates <= 0:
        raise TemplatePolicyValidationError("max_templates must be positive")
    if max_templates_pct is not None and not (1 <= max_templates_pct <= 100):
        raise TemplatePolicyValidationError("max_templates_pct must be between 1 and 100")


def effective_limit(
    estimated_templates: int,
    *,
    max_templates: int | None,
    max_templates_pct: int | None,
) -> int | None:
    """The cap on how many templates one drafting call may produce.

    The smaller of whichever caps are set, never the larger: both fields are
    caps, and a cap that a second cap can raise is not a cap. None means no
    limit at all, only when neither field is set.
    """
    caps = []
    if max_templates is not None:
        caps.append(max_templates)
    if max_templates_pct is not None:
        caps.append(math.ceil(estimated_templates * max_templates_pct / 100))
    return min(caps) if caps else None


def get_campaign_policy(session: Session, campaign_id: int) -> CampaignTemplatePolicy | None:
    """This campaign's own override, or None if it has never set one."""
    return session.get(CampaignTemplatePolicy, campaign_id)


def set_campaign_policy(
    session: Session,
    campaign_id: int,
    *,
    max_templates: int | None,
    max_templates_pct: int | None,
    updated_by: str,
) -> CampaignTemplatePolicy:
    """Create or replace this campaign's own limit.

    A policy override is a live setting a manager can dial up or down, not
    an immutable history like a rule or a config version -- an existing row
    is updated in place. The caller is responsible for confirming the
    campaign itself exists first.
    """
    _validate_limits(max_templates, max_templates_pct)
    policy = session.get(CampaignTemplatePolicy, campaign_id)
    if policy is None:
        policy = CampaignTemplatePolicy(campaign_id=campaign_id)
        session.add(policy)
    policy.max_templates = max_templates
    policy.max_templates_pct = max_templates_pct
    policy.updated_by = updated_by
    session.flush()
    return policy


def active_default_config_version(session: Session, at: date) -> int | None:
    """The default config version in force on `at`, or None if there is none."""
    return session.scalar(
        select(TemplatePolicyConfigVersion.version)
        .where(
            TemplatePolicyConfigVersion.valid_from <= at,
            or_(
                TemplatePolicyConfigVersion.valid_to.is_(None),
                TemplatePolicyConfigVersion.valid_to > at,
            ),
        )
        .order_by(
            TemplatePolicyConfigVersion.valid_from.desc(),
            TemplatePolicyConfigVersion.version.desc(),
        )
        .limit(1)
    )


def load_active_default_config(session: Session, at: date) -> TemplatePolicyConfigVersion | None:
    """The full default config row in force on `at`, or None if there is none."""
    version = active_default_config_version(session, at)
    if version is None:
        return None
    return session.scalar(
        select(TemplatePolicyConfigVersion).where(TemplatePolicyConfigVersion.version == version)
    )


def save_default_config_version(
    session: Session,
    version: int,
    *,
    default_max_templates: int | None,
    default_max_templates_pct: int | None,
    valid_from: date,
    valid_to: date | None = None,
) -> TemplatePolicyConfigVersion:
    """Validate and insert a new default config version.

    Refuses to touch a version that already exists, so a drafting call made
    under the old default stays explainable against exactly what was live
    when it ran. Retuning the default means saving a new version.
    """
    _validate_limits(default_max_templates, default_max_templates_pct)
    if session.scalar(select(func.count()).where(TemplatePolicyConfigVersion.version == version)):
        raise TemplatePolicyValidationError(
            f"version {version} already exists and may not be mutated"
        )
    row = TemplatePolicyConfigVersion(
        version=version,
        default_max_templates=default_max_templates,
        default_max_templates_pct=default_max_templates_pct,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class EffectivePolicy:
    """Which limit fields are actually in force for one campaign, and where
    they came from -- its own override, or the system default.
    """

    source: str  # "campaign" or "default"
    max_templates: int | None
    max_templates_pct: int | None
    updated_at: datetime | None
    updated_by: str | None


def get_effective_policy(
    session: Session, campaign_id: int, *, at: date | None = None
) -> EffectivePolicy:
    """This campaign's own policy row if it has one, else whichever
    template_policy_config_version is active on `at` (today, by default).
    """
    policy = get_campaign_policy(session, campaign_id)
    if policy is not None:
        return EffectivePolicy(
            source="campaign",
            max_templates=policy.max_templates,
            max_templates_pct=policy.max_templates_pct,
            updated_at=policy.updated_at,
            updated_by=policy.updated_by,
        )
    default = load_active_default_config(session, at or date.today())
    if default is None:
        return EffectivePolicy(
            source="default",
            max_templates=None,
            max_templates_pct=None,
            updated_at=None,
            updated_by=None,
        )
    return EffectivePolicy(
        source="default",
        max_templates=default.default_max_templates,
        max_templates_pct=default.default_max_templates_pct,
        updated_at=None,
        updated_by=None,
    )


def resolve_effective_limit(
    session: Session,
    campaign_id: int,
    estimated_templates: int,
    *,
    at: date | None = None,
) -> int | None:
    """The effective_limit a drafting call for this campaign faces right now.

    Reads the campaign's own policy row if it has one; otherwise falls back
    to whichever template_policy_config_version is active on `at` (today,
    by default). None means no limit either way.
    """
    policy = get_effective_policy(session, campaign_id, at=at)
    return effective_limit(
        estimated_templates,
        max_templates=policy.max_templates,
        max_templates_pct=policy.max_templates_pct,
    )
