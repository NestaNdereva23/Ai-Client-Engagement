"""Command line entry point to change when a business-rule version takes
effect, without writing a migration for it.

A rule set's own content (its matches and outputs) is never mutated once
shipped -- save_version refuses to touch an existing version. But its
validity window is operational metadata, not content, the same distinction
message_angle_catalog.held already draws: this changes only valid_from and
(optionally) valid_to on the rows already there.

Bring version 3 live as of today:
    uv run python scripts/activate_rule_version.py --version 3

Schedule it for a specific date, and close version 2's window at the same time:
    uv run python scripts/activate_rule_version.py --version 3 --valid-from 2026-09-01
    uv run python scripts/activate_rule_version.py --version 2 --valid-to 2026-09-01

Clear a version's end date (leave it open-ended again):
    uv run python scripts/activate_rule_version.py --version 2 --valid-to none
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.rules import BusinessRule  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rules.store import load_active_rules  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Change a business-rule version's validity window."
    )
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument(
        "--valid-from",
        type=date.fromisoformat,
        default=date.today(),
        help="ISO date this version starts applying (default: today).",
    )
    parser.add_argument(
        "--valid-to",
        help="ISO date this version stops applying, or 'none' to clear it. "
        "Left unchanged if omitted.",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)
    print(
        f"input: version={args.version} valid_from={args.valid_from} valid_to_arg={args.valid_to!r}"
    )

    with SessionLocal() as session:
        rows = list(
            session.scalars(select(BusinessRule).where(BusinessRule.version == args.version))
        )
        if not rows:
            parser.error(f"no rule version {args.version} exists")

        print(
            f"  {len(rows)} rule(s) currently valid_from={rows[0].valid_from} "
            f"valid_to={rows[0].valid_to}"
        )

        for row in rows:
            row.valid_from = args.valid_from
            if args.valid_to is not None:
                row.valid_to = (
                    None if args.valid_to.lower() == "none" else date.fromisoformat(args.valid_to)
                )
        session.commit()

        print(
            f"output: version {args.version} now valid_from={rows[0].valid_from} "
            f"valid_to={rows[0].valid_to}"
        )

        windows = session.execute(
            select(BusinessRule.version, BusinessRule.valid_from, BusinessRule.valid_to)
            .distinct()
            .order_by(BusinessRule.version)
        ).all()
        print("all versions:")
        for version, valid_from, valid_to in windows:
            print(f"  version={version} valid_from={valid_from} valid_to={valid_to}")

        today = date.today()
        active = load_active_rules(session, today)
        active_version = active[0].version if active else None
        print(f"active version as of today ({today}): {active_version}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
