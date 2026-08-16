"""Wipe campaign/enrollment/outreach data so campaign setups can be re-tested
without re-running ingestion, transform, or indicator resolution.

Leaves untouched: raw_staging, clients, client_fund, transactions,
client_features, client_message_indicators, business_rules,
message_angle_catalog, tier_contract, and the versioned model/prompt/rubric
registries (model_versions, prompt_versions, rubric_versions) -- none of that
is campaign-specific, all of it is expensive to rebuild, and none of it needs
to change between campaign test runs.

Clears: campaign, campaign_step, enrollment, touch_log, outreach_message,
review_action, message_template, message_template_review_action,
generation_batch, generation_batch_item, template_generation_plan,
campaign_template_policy, and the LLM generation history a campaign run
creates (generation_runs, llm_requests, llm_responses, token_usage,
tool_calls, trace_refs, evaluations). contact_events is left alone; it is
keyed by client_id, not campaign, and isn't cleared by this script.

Dry run (default): prints how many rows in each table would be deleted.
    uv run python scripts/inactive/reset_campaign_data.py

Actually delete everything:
    uv run python scripts/inactive/reset_campaign_data.py --yes

Only one campaign, leaving others alone:
    uv run python scripts/inactive/reset_campaign_data.py --campaign-id 3 --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import func, select, true  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog  # noqa: E402
from app.db.models.generation_batch import GenerationBatch, GenerationBatchItem  # noqa: E402
from app.db.models.llmops import (  # noqa: E402
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.message_template import MessageTemplate, TemplateReviewAction  # noqa: E402
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction  # noqa: E402
from app.db.models.template_generation_plan import TemplateGenerationPlan  # noqa: E402
from app.db.models.template_policy import CampaignTemplatePolicy  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


def _in_or_all(column, ids: list | None):
    """column.in_(ids), or an unconditional true() when ids is None (no scoping)."""
    return true() if ids is None else column.in_(ids)


def _resolve_run_ids(session: Session, campaign_ids: list[int]) -> list[str]:
    """generation_runs reachable from these campaigns, via outreach_message or message_template."""
    from_messages = select(OutreachMessage.generation_run_id).where(
        OutreachMessage.campaign_id.in_(campaign_ids)
    )
    from_templates = select(MessageTemplate.generation_run_id).where(
        MessageTemplate.campaign_id.in_(campaign_ids)
    )
    run_ids = set(session.execute(from_messages).scalars())
    run_ids |= set(session.execute(from_templates).scalars())
    return list(run_ids)


def _build_steps(campaign_ids: list[int] | None, run_ids: list[str] | None):
    """One (label, model, where_clause) per table, in child-before-parent order."""
    template_ids = select(MessageTemplate.template_id).where(
        _in_or_all(MessageTemplate.campaign_id, campaign_ids)
    )
    message_ids = select(OutreachMessage.message_id).where(
        _in_or_all(OutreachMessage.campaign_id, campaign_ids)
    )
    enrollment_ids = select(Enrollment.enrollment_id).where(
        _in_or_all(Enrollment.campaign_id, campaign_ids)
    )
    batch_ids = select(GenerationBatch.generation_batch_id).where(
        _in_or_all(GenerationBatch.campaign_id, campaign_ids)
    )
    request_ids = select(LLMRequest.request_id).where(_in_or_all(LLMRequest.run_id, run_ids))

    # Each table below has its own campaign_id column; the filter must bind to
    # that table's column, not a shared expression, or SQLAlchemy adds the
    # `campaign` table into the FROM clause as an unrelated cross join.
    return [
        ("token_usage", TokenUsage, TokenUsage.request_id.in_(request_ids)),
        ("llm_responses", LLMResponse, LLMResponse.request_id.in_(request_ids)),
        ("evaluations", Evaluation, _in_or_all(Evaluation.run_id, run_ids)),
        ("tool_calls", ToolCall, _in_or_all(ToolCall.run_id, run_ids)),
        ("trace_refs", TraceRef, _in_or_all(TraceRef.run_id, run_ids)),
        ("llm_requests", LLMRequest, _in_or_all(LLMRequest.run_id, run_ids)),
        (
            "message_template_review_action",
            TemplateReviewAction,
            TemplateReviewAction.template_id.in_(template_ids),
        ),
        ("review_action", ReviewAction, ReviewAction.message_id.in_(message_ids)),
        ("touch_log", TouchLog, TouchLog.enrollment_id.in_(enrollment_ids)),
        (
            "generation_batch_item",
            GenerationBatchItem,
            GenerationBatchItem.generation_batch_id.in_(batch_ids),
        ),
        (
            "outreach_message",
            OutreachMessage,
            _in_or_all(OutreachMessage.campaign_id, campaign_ids),
        ),
        (
            "message_template",
            MessageTemplate,
            _in_or_all(MessageTemplate.campaign_id, campaign_ids),
        ),
        ("generation_runs", GenerationRun, _in_or_all(GenerationRun.run_id, run_ids)),
        (
            "generation_batch",
            GenerationBatch,
            _in_or_all(GenerationBatch.campaign_id, campaign_ids),
        ),
        (
            "template_generation_plan",
            TemplateGenerationPlan,
            _in_or_all(TemplateGenerationPlan.campaign_id, campaign_ids),
        ),
        (
            "campaign_template_policy",
            CampaignTemplatePolicy,
            _in_or_all(CampaignTemplatePolicy.campaign_id, campaign_ids),
        ),
        ("enrollment", Enrollment, _in_or_all(Enrollment.campaign_id, campaign_ids)),
        ("campaign_step", CampaignStep, _in_or_all(CampaignStep.campaign_id, campaign_ids)),
        ("campaign", Campaign, _in_or_all(Campaign.campaign_id, campaign_ids)),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete campaign/enrollment/outreach data for a clean campaign re-test."
    )
    parser.add_argument(
        "--campaign-id",
        type=int,
        action="append",
        dest="campaign_ids",
        help="Only clear this campaign (repeatable). Omit to clear every campaign.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this flag, only counts are printed.",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        campaign_ids = args.campaign_ids
        run_ids: list[str] | None = None
        if campaign_ids is not None:
            found = set(
                session.execute(
                    select(Campaign.campaign_id).where(Campaign.campaign_id.in_(campaign_ids))
                ).scalars()
            )
            missing = set(campaign_ids) - found
            if missing:
                parser.error(f"no campaign with id(s): {sorted(missing)}")
            run_ids = _resolve_run_ids(session, campaign_ids)

        steps = _build_steps(campaign_ids, run_ids)

        print(f"scope: {'all campaigns' if campaign_ids is None else campaign_ids}")
        print(f"mode:  {'DELETE' if args.yes else 'dry run (pass --yes to actually delete)'}")
        print()

        total = 0
        for label, model, where_clause in steps:
            count = session.execute(
                select(func.count()).select_from(model).where(where_clause)
            ).scalar_one()
            total += count
            print(f"  {label:<32} {count}")
            if args.yes and count:
                session.query(model).filter(where_clause).delete(synchronize_session=False)

        if args.yes:
            session.commit()
            print(f"\ndeleted {total} row(s) total")
        else:
            print(f"\nwould delete {total} row(s) total (dry run, nothing changed)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
