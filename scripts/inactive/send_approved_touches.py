"""Command line entry point to send every approved, unsent touch in one campaign.

Usage:
    uv run python scripts/inactive/send_approved_touches.py --campaign-id 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.campaigns.touch import SendBlocked, send_touch  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.campaigns import Enrollment, TouchLog  # noqa: E402
from app.db.models.outreach import OutreachMessage  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send every approved, unsent touch in one campaign."
    )
    parser.add_argument("--campaign-id", type=int, required=True)
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)
    print(f"input: campaign_id={args.campaign_id}")

    with SessionLocal() as session:
        rows = list(
            session.execute(
                select(TouchLog, Enrollment.client_id, OutreachMessage.ai_draft_content)
                .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
                .join(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
                .where(
                    Enrollment.campaign_id == args.campaign_id,
                    OutreachMessage.status == "approved",
                    TouchLog.sent_at.is_(None),
                )
            ).all()
        )
        print(f"{len(rows)} approved, unsent touch(es) found:")
        for touch, client_id, draft in rows:
            print(
                f"  touch_id={touch.touch_id} enrollment_id={touch.enrollment_id} "
                f"client_id={client_id} subject={draft.get('subject')!r}"
            )

        sent = blocked = 0
        for touch, client_id, _draft in rows:
            print(
                f"\nsending touch {touch.touch_id} "
                f"(enrollment {touch.enrollment_id}, client_id={client_id})"
            )
            try:
                send_touch(session, touch)
                session.commit()
                sent += 1
                print(
                    f"    -> sent: delivery_status={touch.delivery_status!r} "
                    f"sent_at={touch.sent_at}"
                )
            except SendBlocked as exc:
                session.rollback()
                blocked += 1
                print(f"    -> blocked: {exc.reason}")

    print(f"\noutput: {sent} sent, {blocked} blocked, {len(rows)} approved touch(es) considered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
