"""Command line entry point for transforming a staged inactive-clients run.

Flattens raw_staging into clients, client_fund, transactions, pii_vault and
client_features. Only ever picks up an inactive-clients run when guessing the
latest one: a shared "latest run" query once picked up a more recent
active-clients run instead, and quietly loaded the wrong population into
these tables (they hold one dormant client's data, not the other feed's).
Endpoint-scoping the query is what makes that impossible now, and there is a
matching, separate script at scripts/active/transform.py for the other feed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.models import IngestionStatus  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.transform.load import transform_run  # noqa: E402

ENDPOINT = "inactive-clients"


def _latest_run_id(session) -> str | None:
    """The most recently started inactive-clients run, whatever its state --
    transform only needs raw_staging rows, not a "completed" ingestion_status.
    Scoped to this endpoint so a more recent active-clients run is never
    picked up by mistake.
    """
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
        counts = transform_run(session, run_id)

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
