from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.agent_proposal import AgentProposal  # noqa: E402
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan  # noqa: E402
from app.db.models.generation_batch import GenerationBatch, GenerationBatchItem  # noqa: E402
from app.db.models.instantiation_batch import InstantiationBatch  # noqa: E402
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


def _build_steps(campaign_ids: list[int], run_ids: list[str]):
    """One (label, model, where_clause) per table, in child-before-parent order."""
    template_ids = select(MessageTemplate.template_id).where(
        MessageTemplate.campaign_id.in_(campaign_ids)
    )
    message_ids = select(OutreachMessage.message_id).where(
        OutreachMessage.campaign_id.in_(campaign_ids)
    )
    enrollment_ids = select(Enrollment.enrollment_id).where(
        Enrollment.campaign_id.in_(campaign_ids)
    )
    batch_ids = select(GenerationBatch.generation_batch_id).where(
        GenerationBatch.campaign_id.in_(campaign_ids)
    )
    request_ids = select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))

    # Each table below has its own campaign_id column; the filter must bind to
    # that table's column, not a shared expression, or SQLAlchemy adds the
    # `campaign` table into the FROM clause as an unrelated cross join.
    return [
        ("token_usage", TokenUsage, TokenUsage.request_id.in_(request_ids)),
        ("llm_responses", LLMResponse, LLMResponse.request_id.in_(request_ids)),
        ("evaluations", Evaluation, Evaluation.run_id.in_(run_ids)),
        ("tool_calls", ToolCall, ToolCall.run_id.in_(run_ids)),
        ("trace_refs", TraceRef, TraceRef.run_id.in_(run_ids)),
        ("llm_requests", LLMRequest, LLMRequest.run_id.in_(run_ids)),
        (
            "message_template_review_action",
            TemplateReviewAction,
            TemplateReviewAction.template_id.in_(template_ids),
        ),
        ("review_action", ReviewAction, ReviewAction.message_id.in_(message_ids)),
        ("touch_log", TouchLog, TouchLog.enrollment_id.in_(enrollment_ids)),
        ("touch_plan", TouchPlan, TouchPlan.enrollment_id.in_(enrollment_ids)),
        (
            "generation_batch_item",
            GenerationBatchItem,
            GenerationBatchItem.generation_batch_id.in_(batch_ids),
        ),
        (
            "outreach_message",
            OutreachMessage,
            OutreachMessage.campaign_id.in_(campaign_ids),
        ),
        (
            "message_template",
            MessageTemplate,
            MessageTemplate.campaign_id.in_(campaign_ids),
        ),
        ("generation_runs", GenerationRun, GenerationRun.run_id.in_(run_ids)),
        (
            "generation_batch",
            GenerationBatch,
            GenerationBatch.campaign_id.in_(campaign_ids),
        ),
        (
            "template_generation_plan",
            TemplateGenerationPlan,
            TemplateGenerationPlan.campaign_id.in_(campaign_ids),
        ),
        (
            "campaign_template_policy",
            CampaignTemplatePolicy,
            CampaignTemplatePolicy.campaign_id.in_(campaign_ids),
        ),
        ("enrollment", Enrollment, Enrollment.campaign_id.in_(campaign_ids)),
        ("campaign_step", CampaignStep, CampaignStep.campaign_id.in_(campaign_ids)),
        (
            "instantiation_batch",
            InstantiationBatch,
            InstantiationBatch.campaign_id.in_(campaign_ids),
        ),
        # campaign_id is nullable here on purpose: an agent_proposal is the
        # agent's own decision record, not campaign data, so unlinking it
        # clears the FK without erasing that history. Must run before the
        # campaign delete below, or that delete hits this FK and fails.
        (
            "agent_proposal (unlinked)",
            AgentProposal,
            AgentProposal.campaign_id.in_(campaign_ids),
            "null_campaign_id",
        ),
        ("campaign", Campaign, Campaign.campaign_id.in_(campaign_ids)),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the campaign/enrollment/outreach data of the campaigns you name, "
            "for a clean campaign re-test. Other campaigns are never touched."
        )
    )
    parser.add_argument(
        "--campaign-id",
        type=int,
        nargs="+",
        action="extend",
        required=True,
        metavar="ID",
        dest="campaign_ids",
        help="Campaign id(s) to clear: one or several, e.g. --campaign-id 23 24 25. Required.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this flag, only counts are printed.",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        campaign_ids = sorted(set(args.campaign_ids))
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

        print(f"scope: campaigns {campaign_ids}")
        print(f"mode:  {'DELETE' if args.yes else 'dry run (pass --yes to actually delete)'}")
        print()

        total = 0
        for label, model, where_clause, *rest in steps:
            op = rest[0] if rest else "delete"
            count = session.execute(
                select(func.count()).select_from(model).where(where_clause)
            ).scalar_one()
            total += count
            print(f"  {label:<32} {count}")
            if args.yes and count:
                if op == "null_campaign_id":
                    session.query(model).filter(where_clause).update(
                        {"campaign_id": None}, synchronize_session=False
                    )
                else:
                    session.query(model).filter(where_clause).delete(synchronize_session=False)

        if args.yes:
            session.commit()
            print(f"\ndeleted/unlinked {total} row(s) total")
        else:
            print(f"\nwould delete/unlink {total} row(s) total (dry run, nothing changed)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
