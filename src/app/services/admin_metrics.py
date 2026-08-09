"""Admin dashboard metrics: tokens, latency, cost, error rate, and guardrail
failure rate, sliced by angle and tier as well as prompt and model version.

Cost per message is reported per slice rather than as one project-wide
average, because the word caps differ by tier (60 to 140 words), so a single
number would hide the thing worth managing. Guardrail failure rate is
reported per angle for the same reason: a brief that keeps failing the
numeric check is a brief that needs rewriting, and that only shows up once
the angle stops being averaged away with the other eleven.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    ModelVersion,
    PromptVersion,
    TokenUsage,
)
from app.db.models.outreach import MESSAGE_STATUSES, OutreachMessage
from app.llmops.pricing import estimate_cost_usd

_REJECTED = "rejected"


@dataclass(frozen=True)
class RunMetricsRow:
    """One (angle, tier, prompt variant, model) slice's aggregate figures."""

    message_angle: str | None
    priority_tier: str | None
    prompt_variant: str | None
    model_id: str | None
    run_count: int
    error_rate: float
    avg_latency_ms: float | None
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    avg_cost_usd: float | None


@dataclass(frozen=True)
class GuardrailFailureRow:
    """How often one guardrail sinks a given angle's runs."""

    message_angle: str | None
    failed_guardrail: str
    fail_count: int
    run_count: int
    failure_rate: float


@dataclass(frozen=True)
class JudgeScoreRow:
    """One (angle, tier) slice's average judge scores, across every run it has scored."""

    message_angle: str | None
    priority_tier: str | None
    evaluation_count: int
    avg_tone: float
    avg_compliance: float
    avg_grounding: float
    avg_personalization: float


@dataclass(frozen=True)
class FunnelCounts:
    """Book-wide counts through the pipeline that actually exists today.

    Stops at review, not send: there is no delivery tracking yet, so a
    "sent" stage would be invented, not measured. guardrail_rejected and
    review_rejected are kept apart because they are two different kinds of
    rejection (an unguarded draft vs. a human's call on a fine one) that
    happen to share a status string on two different tables.
    """

    generated: int
    accepted: int
    guardrail_rejected: int
    pending_review: int
    approved: int
    review_rejected: int
    escalated: int
    held: int


@dataclass(frozen=True)
class DailyCountRow:
    """One day's generation-run throughput."""

    day: date
    generated: int
    accepted: int


def run_metrics(
    session: Session,
    *,
    message_angle: str | None = None,
    priority_tier: str | None = None,
    prompt_variant: str | None = None,
    model_id: str | None = None,
) -> list[RunMetricsRow]:
    """Tokens, latency, cost, and error rate, grouped by angle, tier, prompt
    variant, and model, optionally narrowed by any of the four.

    Tokens and cost are summed per run before averaging, since "cost per
    message" means the whole run's retries, not one call in isolation.
    Latency is averaged per call, since that is what a single request's
    speed actually measures.
    """
    query = (
        select(GenerationRun, PromptVersion, ModelVersion)
        .join(PromptVersion, GenerationRun.prompt_version_id == PromptVersion.prompt_version_id)
        .join(ModelVersion, GenerationRun.model_version_id == ModelVersion.model_version_id)
    )
    if message_angle is not None:
        query = query.where(PromptVersion.angle == message_angle)
    if priority_tier is not None:
        query = query.where(GenerationRun.priority_tier == priority_tier)
    if prompt_variant is not None:
        query = query.where(PromptVersion.prompt_variant == prompt_variant)
    if model_id is not None:
        query = query.where(ModelVersion.model_id == model_id)

    runs = session.execute(query).all()
    if not runs:
        return []

    run_ids = [run.run_id for run, _prompt_version, _model_version in runs]
    call_rows = session.execute(
        select(
            LLMRequest.run_id,
            LLMResponse.latency_ms,
            TokenUsage.input_tokens,
            TokenUsage.output_tokens,
            ModelVersion.provider,
            ModelVersion.model_id,
        )
        .join(LLMResponse, LLMResponse.request_id == LLMRequest.request_id)
        .outerjoin(TokenUsage, TokenUsage.request_id == LLMRequest.request_id)
        .join(ModelVersion, LLMRequest.model_version_id == ModelVersion.model_version_id)
        .where(LLMRequest.run_id.in_(run_ids))
    ).all()

    latencies_by_run: dict[str, list[int]] = defaultdict(list)
    input_tokens_by_run: dict[str, int] = defaultdict(int)
    output_tokens_by_run: dict[str, int] = defaultdict(int)
    cost_by_run: dict[str, float] = defaultdict(float)
    priced_run_ids: set[str] = set()
    for run_id, latency_ms, input_tokens, output_tokens, provider, call_model_id in call_rows:
        latencies_by_run[run_id].append(latency_ms)
        if input_tokens is not None:
            input_tokens_by_run[run_id] += input_tokens
        if output_tokens is not None:
            output_tokens_by_run[run_id] += output_tokens
        cost = estimate_cost_usd(provider, call_model_id, input_tokens, output_tokens)
        if cost is not None:
            cost_by_run[run_id] += cost
            priced_run_ids.add(run_id)

    groups: dict[tuple[str | None, str | None, str | None, str | None], list[GenerationRun]] = (
        defaultdict(list)
    )
    for run, prompt_version, model_version in runs:
        key = (
            prompt_version.angle,
            run.priority_tier,
            prompt_version.prompt_variant,
            model_version.model_id,
        )
        groups[key].append(run)

    rows = []
    for (angle, tier, variant, group_model_id), group_runs in groups.items():
        run_count = len(group_runs)
        rejected = sum(1 for r in group_runs if r.status == _REJECTED)
        latencies = [ms for r in group_runs for ms in latencies_by_run.get(r.run_id, [])]
        tokens_in = [
            input_tokens_by_run[r.run_id] for r in group_runs if r.run_id in input_tokens_by_run
        ]
        tokens_out = [
            output_tokens_by_run[r.run_id] for r in group_runs if r.run_id in output_tokens_by_run
        ]
        costs = [cost_by_run[r.run_id] for r in group_runs if r.run_id in priced_run_ids]
        rows.append(
            RunMetricsRow(
                message_angle=angle,
                priority_tier=tier,
                prompt_variant=variant,
                model_id=group_model_id,
                run_count=run_count,
                error_rate=rejected / run_count,
                avg_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
                avg_input_tokens=(sum(tokens_in) / len(tokens_in)) if tokens_in else None,
                avg_output_tokens=(sum(tokens_out) / len(tokens_out)) if tokens_out else None,
                avg_cost_usd=(sum(costs) / len(costs)) if costs else None,
            )
        )
    return rows


def guardrail_failure_rates(
    session: Session,
    *,
    message_angle: str | None = None,
) -> list[GuardrailFailureRow]:
    """Per-angle failure rate for each guardrail that has ever sunk a run.

    The denominator is every run for that angle, not just the failed ones,
    so the rate reads as "how often does this angle hit this guardrail",
    the number that flags a brief needing a rewrite.
    """
    query = select(PromptVersion.angle, GenerationRun.failed_guardrail).join(
        PromptVersion, GenerationRun.prompt_version_id == PromptVersion.prompt_version_id
    )
    if message_angle is not None:
        query = query.where(PromptVersion.angle == message_angle)

    totals: dict[str | None, int] = defaultdict(int)
    fails: dict[tuple[str | None, str], int] = defaultdict(int)
    for angle, failed_guardrail in session.execute(query).all():
        totals[angle] += 1
        if failed_guardrail is not None:
            fails[(angle, failed_guardrail)] += 1

    def sort_key(entry: tuple[tuple[str | None, str], int]) -> tuple[str, str]:
        (angle, guardrail), _count = entry
        return (angle or "", guardrail)

    return [
        GuardrailFailureRow(
            message_angle=angle,
            failed_guardrail=guardrail,
            fail_count=count,
            run_count=totals[angle],
            failure_rate=count / totals[angle],
        )
        for (angle, guardrail), count in sorted(fails.items(), key=sort_key)
    ]


def judge_score_metrics(
    session: Session,
    *,
    message_angle: str | None = None,
    priority_tier: str | None = None,
) -> list[JudgeScoreRow]:
    """Average judge scores per angle and tier, across every evaluation so far.

    Unscored runs never enter this average (there is no evaluations row to
    join), which is exactly what ground_truth_rows already surfaces as a
    coverage gap; this endpoint answers a different question, "how good are
    the scored drafts", not "how many drafts got scored".
    """
    query = (
        select(
            PromptVersion.angle,
            GenerationRun.priority_tier,
            Evaluation.tone,
            Evaluation.compliance,
            Evaluation.grounding,
            Evaluation.personalization,
        )
        .select_from(Evaluation)
        .join(GenerationRun, Evaluation.run_id == GenerationRun.run_id)
        .join(PromptVersion, GenerationRun.prompt_version_id == PromptVersion.prompt_version_id)
    )
    if message_angle is not None:
        query = query.where(PromptVersion.angle == message_angle)
    if priority_tier is not None:
        query = query.where(GenerationRun.priority_tier == priority_tier)

    groups: dict[tuple[str | None, str | None], list[tuple[int, int, int, int]]] = defaultdict(list)
    for angle, tier, tone, compliance, grounding, personalization in session.execute(query).all():
        groups[(angle, tier)].append((tone, compliance, grounding, personalization))

    def sort_key(entry: tuple[tuple[str | None, str | None], list]) -> tuple[str, str]:
        (angle, tier), _scores = entry
        return (angle or "", tier or "")

    rows = []
    for (angle, tier), scores in sorted(groups.items(), key=sort_key):
        count = len(scores)
        rows.append(
            JudgeScoreRow(
                message_angle=angle,
                priority_tier=tier,
                evaluation_count=count,
                avg_tone=sum(s[0] for s in scores) / count,
                avg_compliance=sum(s[1] for s in scores) / count,
                avg_grounding=sum(s[2] for s in scores) / count,
                avg_personalization=sum(s[3] for s in scores) / count,
            )
        )
    return rows


def funnel_counts(session: Session) -> FunnelCounts:
    """Book-wide counts at every stage the pipeline has today: generated,
    guardrail-accepted or rejected, then a message's own review outcome.
    """
    generated = session.execute(select(func.count()).select_from(GenerationRun)).scalar_one()
    accepted = session.execute(
        select(func.count()).select_from(GenerationRun).where(GenerationRun.status == "accepted")
    ).scalar_one()
    guardrail_rejected = session.execute(
        select(func.count()).select_from(GenerationRun).where(GenerationRun.status == "rejected")
    ).scalar_one()

    message_counts = dict(
        session.execute(
            select(OutreachMessage.status, func.count()).group_by(OutreachMessage.status)
        ).all()
    )
    by_status = {status: message_counts.get(status, 0) for status in MESSAGE_STATUSES}

    return FunnelCounts(
        generated=generated,
        accepted=accepted,
        guardrail_rejected=guardrail_rejected,
        pending_review=by_status["pending_review"],
        approved=by_status["approved"],
        review_rejected=by_status["rejected"],
        escalated=by_status["escalated"],
        held=by_status["held"],
    )


def daily_generation_counts(session: Session, *, days: int = 30) -> list[DailyCountRow]:
    """Per-day generation counts for the last `days` days, oldest first.

    Bucketed on the day GenerationRun.created_at falls on in the session's
    configured time zone (see app.db.session), the same anchor every other
    date derivation in this codebase uses, rather than UTC.
    """
    since = date.today() - timedelta(days=days - 1)
    day = func.date(GenerationRun.created_at)
    is_accepted = GenerationRun.status == "accepted"
    query = (
        select(
            day.label("day"),
            func.count().label("generated"),
            func.count().filter(is_accepted).label("accepted"),
        )
        .where(day >= since)
        .group_by(day)
        .order_by(day)
    )
    rows = session.execute(query).all()
    return [DailyCountRow(day=r.day, generated=r.generated, accepted=r.accepted) for r in rows]
