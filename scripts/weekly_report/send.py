"""Build and send the weekly follow-up reports.

Send them now, for the seven days before today:
    uv run python scripts/weekly_report/send.py

Build them as of another day:
    uv run python scripts/weekly_report/send.py --date 2026-11-16

Send again to people who already got today's report:
    uv run python scripts/weekly_report/send.py --resend

Write the PDFs to a folder instead of sending them:
    uv run python scripts/weekly_report/send.py --save-to /tmp/reports
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.briefing.weekly_report_pdf import (  # noqa: E402
    render_advisor_report,
    render_lead_report,
)
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.services.client_touchpoints import today_in_nairobi  # noqa: E402
from app.services.user_access import load_advisors  # noqa: E402
from app.services.weekly_report import build_weekly_reports, report_week  # noqa: E402
from app.workers.weekly_report import send_weekly_reports  # noqa: E402


def _save(today: date, folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        reports = [
            report
            for report in build_weekly_reports(session, load_advisors(session), *report_week(today))
            if report.book_clients > 0
        ]
    if not reports:
        print("No advisor has clients, nothing to build.")
        return 0
    for report in reports:
        name = report.advisor.email.split("@")[0]
        (folder / f"advisor-{name}.pdf").write_bytes(render_advisor_report(report))
    (folder / "lead.pdf").write_bytes(render_lead_report(reports))
    print(f"Wrote {len(reports)} advisor report(s) and the lead report to {folder}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and send the weekly follow-up reports.")
    parser.add_argument(
        "--date", type=date.fromisoformat, help="Build as of this day (default: today)."
    )
    parser.add_argument(
        "--save-to", type=Path, help="Write the PDFs to this folder and send nothing."
    )
    parser.add_argument(
        "--resend", action="store_true", help="Send even to people already sent to today."
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)
    today = args.date or today_in_nairobi()
    if args.save_to is not None:
        return _save(today, args.save_to)

    with SessionLocal() as session:
        result = send_weekly_reports(session, today=today, any_day=True, resend=args.resend)
    print(f"sent={result.sent} already_sent={result.already_sent} failed={result.failed}")
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
