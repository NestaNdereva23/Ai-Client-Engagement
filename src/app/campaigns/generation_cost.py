"""What one campaign's drafting would cost, at the active RAG-enabled rate.

One generation call costs the same rate whether it drafts one client's
message or one bucket's template -- both make exactly one call through the
same generation graph (app.agents.graph, app.campaigns.template_generation)
-- so estimate_generation_cost multiplies the same per-generation rate by
two different counts: enrolled clients for single generation, estimated
templates for the subgroup-template path.

The rate itself depends on which model would draft: MODEL_LABELS lists the
models a campaign could be pointed at, each with its own versioned rate (see
app.db.models.generation_cost). Picking a pricier model is the "maximum per
generation" lever -- Fable 5 prices the same campaign roughly ten times over
Haiku 4.5, and that comparison is the point of exposing the model at all.

Per-step and total-for-sequence figures assume every enrolled client reaches
every step in the campaign's sequence, and that each future step's template
count looks like the current due batch's. Neither is a promise -- it is the
same upper-bound, planning-only assumption campaign_value already applies to
cohort value, applied here to cost instead of revenue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.campaigns.estimation import DEFAULT_ESTIMATE_LIMIT, estimate_templates_sql
from app.db.models.campaigns import CampaignStep, Enrollment
from app.db.models.generation_cost import GenerationCostConfigVersion

# Display label for every model a campaign's generation cost can be priced
# against, in the order the UI should offer them. The dict's keys are the
# only model ids active_generation_cost_config and estimate_generation_cost
# accept -- add a model here (and seed its rate in a migration) before it
# can be selected anywhere else.
MODEL_LABELS: dict[str, str] = {
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-fable-5": "Claude Fable 5",
}

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class UnknownGenerationModel(Exception):
    """The requested model isn't one estimate_generation_cost can price."""


class GenerationCostConfigMissing(Exception):
    """No generation_cost_config_version is active for this model/date."""


def active_generation_cost_config(
    session: Session, model: str = DEFAULT_MODEL, at: date | None = None
) -> GenerationCostConfigVersion:
    """The rate in force for `model` on `at` (today, by default).

    Raises UnknownGenerationModel if `model` isn't in MODEL_LABELS, and
    GenerationCostConfigMissing if that model has no version whose
    valid_from/valid_to window covers `at` -- there is no "no rate" default
    the way there is for template limits, since a null cost would silently
    price every campaign at zero.
    """
    if model not in MODEL_LABELS:
        raise UnknownGenerationModel(model)
    at = at or date.today()
    config = session.scalar(
        select(GenerationCostConfigVersion)
        .where(
            GenerationCostConfigVersion.model == model,
            GenerationCostConfigVersion.valid_from <= at,
            or_(
                GenerationCostConfigVersion.valid_to.is_(None),
                GenerationCostConfigVersion.valid_to > at,
            ),
        )
        .order_by(
            GenerationCostConfigVersion.valid_from.desc(),
            GenerationCostConfigVersion.version.desc(),
        )
        .limit(1)
    )
    if config is None:
        raise GenerationCostConfigMissing(model, at)
    return config


def list_generation_cost_models(
    session: Session, at: date | None = None
) -> list[GenerationCostConfigVersion]:
    """The active rate for every model in MODEL_LABELS, in that same order.

    A model with no version covering `at` is left out rather than raising --
    this backs a picker, and a model missing a rate just shouldn't be
    offered yet.
    """
    configs = []
    for model in MODEL_LABELS:
        try:
            configs.append(active_generation_cost_config(session, model, at))
        except GenerationCostConfigMissing:
            continue
    return configs


@dataclass(frozen=True)
class CostScenario:
    """One drafting mode's per-step and full-sequence cost, at the active rate.

    count_per_step is generation calls, not clients or messages: one call
    per client for single_generation, one call per profile bucket for
    templates. total_cost assumes every step in the sequence costs the same
    as the one step this estimate priced -- see the module docstring.
    """

    count_per_step: int
    cost_per_step_usd: float
    cost_per_step_kes: float
    total_cost_usd: float
    total_cost_kes: float


@dataclass(frozen=True)
class CampaignCostEstimate:
    """What drafting this campaign would cost, at the active rate for `model`.

    enrolled_clients is the same primary-row, "one person counts once" scope
    campaign_value already uses for cohort value; estimated_templates is
    whatever GET .../templates/estimate would return right now. A campaign
    with no steps yet prices at zero regardless of cohort size.
    """

    campaign_id: int
    model: str
    config_version: int
    rate_per_generation_usd: float
    rate_per_generation_kes: float
    step_count: int
    enrolled_clients: int
    estimated_templates: int
    single_generation: CostScenario
    templates: CostScenario
    as_of: datetime


def _enrolled_client_count(session: Session, campaign_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.campaign_id == campaign_id,
            Enrollment.is_primary_contact_row.is_(True),
        )
    ).scalar_one()


def _step_count(session: Session, campaign_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(CampaignStep)
        .where(CampaignStep.campaign_id == campaign_id)
    ).scalar_one()


def estimate_generation_cost(
    session: Session,
    campaign_id: int,
    *,
    model: str = DEFAULT_MODEL,
    limit: int = DEFAULT_ESTIMATE_LIMIT,
    at: date | None = None,
) -> CampaignCostEstimate:
    """Price both drafting modes for one campaign, at `model`'s rate on `at`.

    Read-only: reuses estimate_templates_sql for the template count and
    plain counts for enrollment and step totals, so it changes nothing and
    is safe to call any time. Raises UnknownGenerationModel if `model` isn't
    supported and GenerationCostConfigMissing if it has no active rate; the
    caller is responsible for confirming the campaign itself exists first
    (estimate_templates_sql tolerates an unknown campaign by returning an
    empty estimate rather than raising).
    """
    config = active_generation_cost_config(session, model, at)
    template_estimate = estimate_templates_sql(session, campaign_id, limit=limit)
    enrolled = _enrolled_client_count(session, campaign_id)
    steps = _step_count(session, campaign_id)

    def scenario(count_per_step: int) -> CostScenario:
        usd_per_step = count_per_step * config.cost_per_generation_usd
        kes_per_step = count_per_step * config.cost_per_generation_kes
        return CostScenario(
            count_per_step=count_per_step,
            cost_per_step_usd=usd_per_step,
            cost_per_step_kes=kes_per_step,
            total_cost_usd=usd_per_step * steps,
            total_cost_kes=kes_per_step * steps,
        )

    return CampaignCostEstimate(
        campaign_id=campaign_id,
        model=config.model,
        config_version=config.version,
        rate_per_generation_usd=config.cost_per_generation_usd,
        rate_per_generation_kes=config.cost_per_generation_kes,
        step_count=steps,
        enrolled_clients=enrolled,
        estimated_templates=template_estimate.estimated_templates,
        single_generation=scenario(enrolled),
        templates=scenario(template_estimate.estimated_templates),
        as_of=template_estimate.as_of,
    )
