from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import structlog  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.models import IngestionStatus  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.transform.load import transform_run  # noqa: E402

logger = structlog.get_logger(__name__)

ENDPOINT = "inactive-clients"


def _latest_run_id(session) -> str | None:
    return session.execute(
        select(IngestionStatus.run_id)
        .where(IngestionStatus.endpoint == ENDPOINT)
        .order_by(IngestionStatus.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flatten a staged inactive-clients run into the normalized tables."
    )
    parser.add_argument("--run-id", help="Transform this run instead of the most recent one.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    with SessionLocal() as session:
        run_id = args.run_id or _latest_run_id(session)
        if run_id is None:
            parser.error(
                "no inactive-clients ingestion run found; "
                "run scripts/inactive/ingest.py first, or pass --run-id"
            )

        print(f"transforming run {run_id}")
        try:
            counts = transform_run(session, run_id)
        except Exception:
            logger.error("transform.failed", run_id=run_id)
            raise

    print(
        f"funds={counts.funds} clients={counts.clients} client_funds={counts.client_funds} "
        f"transactions={counts.transactions} vault={counts.vault} features={counts.features}"
    )
    if counts.client_funds > counts.clients:
        held = counts.client_funds - counts.clients
        print(f"{held} clients hold more than one fund; each is contacted on their largest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
