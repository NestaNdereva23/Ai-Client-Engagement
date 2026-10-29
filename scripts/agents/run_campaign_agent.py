from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.agents.email_channel import build_default_orchestrator  # noqa: E402
from app.campaigns.generation import model_boundary_audit_sink  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog, TouchPlan  # noqa: E402
from app.db.models.outreach import Campaign, OutreachMessage  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.services.campaigns import plan_and_run_due_enrollments  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a throwaway test campaign over real clients, run the campaign "
            "agent and generation against it, and print what it planned and drafted."
        )
    )
    parser.add_argument(
        "--client-id",
        type=int,
        action="append",
        required=True,
        dest="client_ids",
        help="A real, already-ingested client_id to enroll. Give this flag more than once.",
    )
    parser.add_argument("--steps", type=int, default=3, help="How many campaign steps to create.")
    parser.add_argument("--min-gap-days", type=int, default=2)
    parser.add_argument("--max-gap-days", type=int, default=10)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    session = SessionLocal()
    try:
        campaign = Campaign(
            name="campaign agent manual test run",
            is_test=True,
            planning_mode="agent",
            agent_touch_policy={
                "min_gap_days": args.min_gap_days,
                "max_gap_days": args.max_gap_days,
            },
        )
        session.add(campaign)
        session.commit()
        print(f"campaign_id={campaign.campaign_id}")

        for step_no in range(1, args.steps + 1):
            session.add(
                CampaignStep(
                    campaign_id=campaign.campaign_id, step_no=step_no, offset_days=step_no * 7
                )
            )
        session.commit()

        for client_id in args.client_ids:
            session.add(Enrollment(campaign_id=campaign.campaign_id, client_id=client_id))
        session.commit()
        print(f"enrolled: {args.client_ids}")

        tracer = get_tracer(settings)
        orchestrator = build_default_orchestrator(
            session, settings, audit=model_boundary_audit_sink(session), tracer=tracer
        )
        try:
            outcomes = plan_and_run_due_enrollments(
                session, campaign.campaign_id, orchestrator=orchestrator, settings=settings
            )
        finally:
            tracer.shutdown()

        print("\n--- touch_plan decisions ---")
        plans = session.scalars(
            select(TouchPlan).where(
                TouchPlan.enrollment_id.in_(
                    select(Enrollment.enrollment_id).where(
                        Enrollment.campaign_id == campaign.campaign_id
                    )
                )
            )
        ).all()
        for plan in plans:
            print(
                f"  enrollment_id={plan.enrollment_id} step_no={plan.step_no} "
                f"angle={plan.angle!r} gap_days={plan.gap_days} reason={plan.reason!r}"
            )
            print(f"    next_time={plan.next_time_note!r} cited_facts={plan.cited_facts}")

        print("\n--- drafted messages ---")
        for outcome in outcomes:
            print(
                f"  enrollment_id={outcome.enrollment_id} generated={outcome.generated} "
                f"reason={outcome.reason!r}"
            )
            if outcome.touch_id is None:
                continue
            touch = session.get(TouchLog, outcome.touch_id)
            if touch is None or touch.message_id is None:
                continue
            draft = session.scalar(
                select(OutreachMessage.ai_draft_content).where(
                    OutreachMessage.message_id == touch.message_id
                )
            )
            print(f"    draft: {draft}")

        print(
            "\nNothing was sent. These drafts sit pending review, same as any other campaign touch."
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
