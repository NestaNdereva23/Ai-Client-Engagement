from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


def main(argv: list[str] | None = None) -> int:
    from app.agents.lifecycle_rules import run_lifecycle_rules
    from app.db.session import SessionLocal
    from app.logging_config import configure_logging

    parser = argparse.ArgumentParser(
        description=(
            "Check every finding about a change of client label that no rule has looked at yet. "
            "A rule set to act alone makes the change. A rule that needs a person leaves the "
            "finding waiting. A change with no rule is left as a note. Safe to run again."
        )
    )
    parser.add_argument("--run-id", type=int, help="Only look at findings from this agent run.")
    args = parser.parse_args(argv)

    configure_logging()
    with SessionLocal() as session:
        outcomes = run_lifecycle_rules(session, run_id=args.run_id)
        session.commit()
    if not outcomes:
        print("Nothing was waiting for a rule to look at.")
    for outcome, count in sorted(outcomes.items()):
        print(f"{outcome}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
