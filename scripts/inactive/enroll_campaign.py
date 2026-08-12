"""Command line entry point to create (or reuse) a campaign and enroll clients.

Create a new one-step campaign and enroll two clients:
    uv run python scripts/inactive/enroll_campaign.py --campaign-name "manual test" \
        --client-id 1001 --client-id 1002

Enroll more clients into an existing campaign:
    uv run python scripts/inactive/enroll_campaign.py --campaign-id 3 --client-id 1003
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.campaigns.enrollment import enroll_cohort  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.campaigns import CampaignStep  # noqa: E402
from app.db.models.outreach import Campaign  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or reuse a campaign and enroll a cohort of client_ids."
    )
    parser.add_argument("--campaign-id", type=int, help="Enroll into this existing campaign.")
    parser.add_argument("--campaign-name", help="Create a new one-step campaign with this name.")
    parser.add_argument(
        "--client-id",
        type=int,
        action="append",
        required=True,
        dest="client_ids",
        help="A client_id to enroll (repeatable).",
    )
    args = parser.parse_args(argv)

    if not args.campaign_id and not args.campaign_name:
        parser.error("pass --campaign-id to reuse a campaign, or --campaign-name to create one")

    configure_logging(get_settings().log_level)
    print(f"input: client_ids={args.client_ids}")

    with SessionLocal() as session:
        if args.campaign_id:
            campaign = session.get(Campaign, args.campaign_id)
            if campaign is None:
                parser.error(f"no campaign with id {args.campaign_id}")
            campaign_id = campaign.campaign_id
            print(f"reusing campaign {campaign_id} ({campaign.name!r}, status={campaign.status!r})")
        else:
            campaign = Campaign(name=args.campaign_name, status="running")
            session.add(campaign)
            session.flush()
            campaign_id = campaign.campaign_id
            session.add(
                CampaignStep(
                    campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="mixed"
                )
            )
            session.commit()
            print(f"created campaign {campaign_id} ({args.campaign_name!r}, status='running')")
            print("  added step 1 (offset_days=0, message_angle='mixed')")

        print(f"enrolling {len(args.client_ids)} client_id(s) into campaign {campaign_id}")
        enrolled = enroll_cohort(session, campaign_id=campaign_id, client_ids=args.client_ids)
        session.commit()

    print(f"output: {len(enrolled)} enrollment row(s)")
    for row in enrolled:
        role = "primary" if row.is_primary_contact_row else "suppressed"
        print(f"  enrollment_id={row.enrollment_id} client_id={row.client_id} -> {role}")

    primary = [row.client_id for row in enrolled if row.is_primary_contact_row]
    suppressed = [row.client_id for row in enrolled if not row.is_primary_contact_row]
    print(f"  primary: {primary}")
    if suppressed:
        print(f"  suppressed (a primary row for the same person already exists): {suppressed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
