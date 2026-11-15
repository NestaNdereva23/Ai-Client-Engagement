from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.agents.campaign_suggester import execute_suggestion_run, start_suggestion_run  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.services.campaign_suggestions import list_pending_suggestions  # noqa: E402


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    session = SessionLocal()
    tracer = get_tracer(settings)
    try:
        run = start_suggestion_run(session)
        print(f"run_id={run.run_id}")
        try:
            run = execute_suggestion_run(session, run.run_id, tracer=tracer)
        finally:
            tracer.shutdown()
        print(f"state={run.state} summary={run.summary!r}")

        print("\n--- pending suggestions ---")
        for item in list_pending_suggestions(session):
            draft = item.draft
            print(
                f"  campaign_id={item.campaign_id} name={draft.name!r} "
                f"objective={draft.plan_config.objective} waiting_now={item.waiting_now} "
                f"touches={draft.touch_offsets} problems={item.problems}"
            )
            print(f"    group={draft.cohort.model_dump(exclude_none=True)} reason={draft.reason!r}")

        print("\nNobody is enrolled or contacted until a person approves a suggestion.")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
