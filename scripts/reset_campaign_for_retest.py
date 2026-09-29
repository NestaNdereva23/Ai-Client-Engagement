"""Reset a test campaign's review and send state for a repeat test run.

Unlike scripts/inactive/reset_campaign_data.py, this does not delete the
outreach_message or generation_runs rows -- those hold the ai_draft_content
an LLM call already paid for. This only rewinds what happens to a draft
after it exists: it puts every message back to pending_review or straight
to approved (see --status), clears review_action history, clears
touch_log's sent/delivery fields (without touching message_id, so nothing
looks ungenerated), rewinds each enrollment back to step 0, and puts the
campaign back to draft. The review -> approve -> send cycle -- or just the
send step, if --status approved skips re-review -- can then be run again
for free.

Refuses a campaign that isn't marked is_test unless --force-live is given,
so this can't be pointed at a real campaign by mistake.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.campaigns import Enrollment, TouchLog  # noqa: E402
from app.db.models.outreach import (  # noqa: E402
    Campaign,
    OutreachMessage,
    ReviewAction,
    ReviewCohort,
)
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402

RESET_STATUSES = ("pending_review", "approved")


def _reset_campaign(
    session: Session, campaign: Campaign, *, target_status: str, apply: bool
) -> dict[str, int]:
    message_ids = select(OutreachMessage.message_id).where(
        OutreachMessage.campaign_id == campaign.campaign_id
    )
    enrollment_ids = select(Enrollment.enrollment_id).where(
        Enrollment.campaign_id == campaign.campaign_id
    )

    counts = {
        "outreach_message": session.execute(
            select(func.count())
            .select_from(OutreachMessage)
            .where(OutreachMessage.campaign_id == campaign.campaign_id)
        ).scalar_one(),
        "review_action": session.execute(
            select(func.count())
            .select_from(ReviewAction)
            .where(ReviewAction.message_id.in_(message_ids))
        ).scalar_one(),
        "touch_log_sent": session.execute(
            select(func.count())
            .select_from(TouchLog)
            .where(TouchLog.enrollment_id.in_(enrollment_ids), TouchLog.sent_at.isnot(None))
        ).scalar_one(),
        "enrollment": session.execute(
            select(func.count())
            .select_from(Enrollment)
            .where(Enrollment.campaign_id == campaign.campaign_id)
        ).scalar_one(),
    }

    if not apply:
        return counts

    session.query(ReviewAction).filter(ReviewAction.message_id.in_(message_ids)).delete(
        synchronize_session=False
    )
    session.query(OutreachMessage).filter(
        OutreachMessage.campaign_id == campaign.campaign_id
    ).update({"status": target_status}, synchronize_session=False)
    session.query(TouchLog).filter(TouchLog.enrollment_id.in_(enrollment_ids)).update(
        {
            "sent_at": None,
            "delivery_status": None,
            "provider_status": None,
            "parts": None,
            "cost": None,
        },
        synchronize_session=False,
    )
    session.query(Enrollment).filter(Enrollment.campaign_id == campaign.campaign_id).update(
        {"status": "enrolled", "current_step": 0, "next_due_at": None},
        synchronize_session=False,
    )
    # approved skips re-review entirely, so the cohort is done, not sampling again.
    cohort_values = (
        {"status": "completed", "completed_at": datetime.now(UTC)}
        if target_status == "approved"
        else {"status": "sampling", "completed_at": None}
    )
    session.query(ReviewCohort).filter(ReviewCohort.campaign_id == campaign.campaign_id).update(
        cohort_values, synchronize_session=False
    )
    campaign.status = "draft"
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reset a test campaign's review/send state so it can be reviewed "
            "and sent again, without regenerating its drafts."
        )
    )
    parser.add_argument(
        "--campaign-id",
        type=int,
        action="append",
        dest="campaign_ids",
        required=True,
        help="Campaign to reset (repeatable).",
    )
    parser.add_argument(
        "--status",
        choices=RESET_STATUSES,
        default="pending_review",
        help=(
            "State to put every message back in. pending_review (default) "
            "re-exercises review too; approved skips straight to resendable."
        ),
    )
    parser.add_argument(
        "--force-live",
        action="store_true",
        help="Allow resetting a campaign that isn't marked is_test.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually apply the reset. Without this flag, only counts are printed.",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        campaigns = list(
            session.execute(
                select(Campaign).where(Campaign.campaign_id.in_(args.campaign_ids))
            ).scalars()
        )
        found_ids = {c.campaign_id for c in campaigns}
        missing = set(args.campaign_ids) - found_ids
        if missing:
            parser.error(f"no campaign with id(s): {sorted(missing)}")

        not_test = sorted(c.campaign_id for c in campaigns if not c.is_test)
        if not_test and not args.force_live:
            parser.error(
                f"campaign(s) {not_test} are not marked is_test; pass --force-live to reset anyway"
            )

        print(f"mode: {'RESET' if args.yes else 'dry run (pass --yes to actually reset)'}")
        print(f"target message status: {args.status}")
        print()

        for campaign in campaigns:
            counts = _reset_campaign(session, campaign, target_status=args.status, apply=args.yes)
            print(f"campaign {campaign.campaign_id} ({campaign.name!r}):")
            for label, count in counts.items():
                print(f"  {label:<18} {count}")
            print()

        if args.yes:
            session.commit()
            print("reset complete -- drafts and generation runs were left untouched")
        else:
            print("nothing changed (dry run)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
