"""Command line entry point for transforming a staged active-clients run.

Run:
    uv run python scripts/transform_active.py --run-id <id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.models import IngestionStatus  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.transform.active_load import transform_active_run  # noqa: E402


def _latest_active_run_id(session) -> str | None:
    """The most recently started active-clients run, whatever its state."""
    return session.execute(
        select(IngestionStatus.run_id)
        .where(IngestionStatus.endpoint == "active-clients")
        .order_by(IngestionStatus.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flatten a staged active-clients run into active_client_fund."
    )
    parser.add_argument("--run-id", help="Transform this run instead of the most recent one.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    with SessionLocal() as session:
        run_id = args.run_id or _latest_active_run_id(session)
        if run_id is None:
            parser.error(
                "no active-clients ingestion run found; "
                "run scripts/ingest.py --endpoint active-clients first, or pass --run-id"
            )

        print(f"transforming active-clients run {run_id}")
        counts = transform_active_run(session, run_id)

    print(f"client_funds={counts.client_funds} vault={counts.vault}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
