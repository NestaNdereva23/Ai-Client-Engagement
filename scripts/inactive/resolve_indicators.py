from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rules.indicators import populate_indicators  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve every client's angle and prompt variant against the active rules."
    )
    parser.add_argument(
        "--at",
        type=date.fromisoformat,
        default=date.today(),
        help="Resolve against the rule version active on this date (default: today).",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    with SessionLocal() as session:
        count = populate_indicators(session, at=args.at)

    print(f"resolved {count} client(s) as of {args.at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
