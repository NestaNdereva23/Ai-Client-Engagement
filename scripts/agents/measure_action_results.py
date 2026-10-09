from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


def main(argv: list[str] | None = None) -> int:
    from app.agents.action_results import measure_action_results
    from app.db.session import SessionLocal
    from app.logging_config import configure_logging

    parser = argparse.ArgumentParser(
        description=(
            "Measure what each sent message led to, once its window is over. "
            "Messages already measured for a window are left alone unless "
            "--remeasure is given."
        )
    )
    parser.add_argument(
        "--remeasure",
        action="store_true",
        help="Measure again the messages that already have a row, and update that row.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    with SessionLocal() as session:
        outcomes = measure_action_results(session, remeasure=args.remeasure)

    for outcome in outcomes:
        print(f"{outcome.window_days} day window: {outcome.measured} messages measured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
