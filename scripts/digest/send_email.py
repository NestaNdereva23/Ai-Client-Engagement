"""Command line entry point for the morning digest emails.

Send them for a digest the nightly job already built:
    uv run python scripts/digest/send_email.py --digest-run-id 42

Print one advisor's email instead of sending it, for tuning the wording
without re-running the nightly job:
    uv run python scripts/digest/send_email.py --digest-run-id 42 --fa-id 3 --dry-run

A dry run sends nothing, writes no marker row, and audits nothing, so it can
be repeated as often as you like. A real run is protected by the marker
table: an advisor already mailed for this digest run is skipped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.workers.digest_email import (  # noqa: E402
    DigestEmailAborted,
    build_advisor_emails,
    send_digest_emails,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the morning digest emails.")
    parser.add_argument("--digest-run-id", type=int, required=True, help="Which digest to send.")
    parser.add_argument("--fa-id", type=int, help="Only this advisor, instead of the whole roster.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the emails instead of sending them. Writes nothing.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    roster = settings.fa_records
    if not roster:
        parser.error("ACE_FA_ROSTER must be set in the environment.")

    with SessionLocal() as session:
        try:
            if args.dry_run:
                emails = build_advisor_emails(
                    session,
                    args.digest_run_id,
                    roster=roster,
                    only_fa_id=args.fa_id,
                    console_base_url=settings.console_base_url,
                )
            else:
                result = send_digest_emails(
                    session,
                    args.digest_run_id,
                    roster=roster,
                    only_fa_id=args.fa_id,
                    settings=settings,
                )
        except DigestEmailAborted as exc:
            print(f"Digest email aborted: {exc}", file=sys.stderr)
            return 2

        if args.dry_run:
            for email in emails:
                print(f"=== to {email.advisor.email} ===")
                print(f"Subject: {email.rendered.subject}")
                print()
                print(email.rendered.text_body)
            print(f"{len(emails)} email(s) rendered, none sent.")
            return 0

    print(
        f"Digest {result.digest_run_id}: {result.sent} sent, "
        f"{result.already_sent} already sent, {result.failed} failed, "
        f"out of {result.advisors} advisor(s)."
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
