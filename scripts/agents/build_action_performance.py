from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


def main(argv: list[str] | None = None) -> int:
    from app.agents.action_performance import build_action_performance
    from app.config import get_settings
    from app.db.session import SessionLocal
    from app.logging_config import configure_logging

    parser = argparse.ArgumentParser(
        description=(
            "Rebuild the results summary from the measured messages. Each row covers one "
            "period, set by ACTION_PERFORMANCE_PERIOD (for example 12h or 7d). A period is "
            "included once it is over and its result window has passed. Safe to run as "
            "often as you like."
        )
    )
    parser.parse_args(argv)

    configure_logging()
    period_hours = get_settings().action_performance_period_hours
    with SessionLocal() as session:
        outcomes = build_action_performance(session)

    print(f"Period length: {period_hours} hours")
    for outcome in outcomes:
        print(f"{outcome.window_days} day window: {outcome.rows} summary rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
