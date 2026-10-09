from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


def main(argv: list[str] | None = None) -> int:
    from datetime import UTC, datetime

    from app.agents.pattern_search import run_pattern_search
    from app.agents.pattern_writeup import write_up_patterns
    from app.db.session import SessionLocal
    from app.logging_config import configure_logging
    from app.privacy.llm_client import get_agent_llm_client

    parser = argparse.ArgumentParser(
        description=(
            "Look for groups of past messages whose results stand out, and write each new one "
            "up for a person to review. Finding the groups uses counting only. The model is "
            "used afterwards, to write up what was found. Safe to run as often as you like, "
            "and meant to run once a week."
        )
    )
    parser.add_argument(
        "--window-days",
        type=int,
        help="Which result window to look at. Defaults to the one the agents read.",
    )
    parser.add_argument(
        "--skip-write-up",
        action="store_true",
        help="Find and save the patterns but do not ask the model to write them up.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    now = datetime.now(UTC)
    with SessionLocal() as session:
        result = run_pattern_search(session, now=now, window_days=args.window_days)
        print(f"Messages measured: {result.measured_messages}")
        print(f"Clients left alone, used as the comparison: {result.not_messaged_clients}")
        print(f"Patterns found: {result.found} ({result.new} new)")
        if args.skip_write_up:
            return 0
        written = write_up_patterns(session, get_agent_llm_client(), seen_at=now)
        print(f"Written up: {written.written}, not written up: {written.failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
