"""Run the whole dormant-book pipeline in one process: ingest, transform, resolve indicators.

Same as running these three in order:
    uv run python scripts/inactive/ingest.py
    uv run python scripts/inactive/transform.py
    uv run python scripts/inactive/resolve_indicators.py

Default fetch is paginated: one page at a time, written to the database as it
goes, resumable if it stops partway. Pass --fast to fetch pages concurrently
and write once at the end instead; quicker, but nothing is saved if it fails
partway, so a failed --fast run has to start over.

    uv run python scripts/inactive/run_pipeline.py
    uv run python scripts/inactive/run_pipeline.py --fast
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.ingestion.api_client import CytonnClient  # noqa: E402
from app.ingestion.endpoints import resolve_endpoint  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rules.indicators import populate_indicators  # noqa: E402
from app.transform.load import transform_run  # noqa: E402
from app.workers.ingestion import IngestionAborted, IngestionWorker  # noqa: E402

ENDPOINT = "inactive-clients"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest, transform, and resolve indicators for the dormant book in one run."
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Fetch pages concurrently and write once at the end instead of one page at a time.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Concurrent page fetches to run with --fast (default: 8).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Stop ingestion after this many pages (safety cap).",
    )
    parser.add_argument(
        "--at",
        type=date.fromisoformat,
        default=date.today(),
        help="Resolve indicators against the rule version active on this date (default: today).",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.cytonn_api_base_url or not settings.cytonn_api_key:
        parser.error("CY_API_BASE_URL and CY_API_KEY must be set in the environment.")

    config = resolve_endpoint(ENDPOINT, settings)
    client = CytonnClient(settings.cytonn_api_base_url, settings.cytonn_api_key)
    worker = IngestionWorker(
        client,
        endpoint=ENDPOINT,
        fetch_path=config.fetch_path,
        max_pages=args.max_pages,
        fund_model=config.fund_model,
        client_model=config.client_model,
        schema_drift_fn=config.schema_drift_fn,
        count_field=config.count_field,
    )

    try:
        if args.fast:
            ingest_result = worker.run_bulk(max_workers=args.workers)
        else:
            ingest_result = worker.run()
    except IngestionAborted as exc:
        print(f"Ingestion aborted: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    print(
        f"ingest: run {ingest_result.run_id} {ingest_result.state}: "
        f"{ingest_result.pages} page(s), {ingest_result.records_seen} seen, "
        f"{ingest_result.records_written} written, "
        f"{ingest_result.records_rejected} rejected, "
        f"shortfall {ingest_result.shortfall}."
    )
    if ingest_result.population_total is not None:
        print(
            f"ingest: population reconciliation: meta.total {ingest_result.population_total}, "
            f"records_seen {ingest_result.records_seen}, gap {ingest_result.population_gap}."
        )

    with SessionLocal() as session:
        print(f"transform: transforming run {ingest_result.run_id}")
        counts = transform_run(session, ingest_result.run_id)
        print(
            f"transform: funds={counts.funds} clients={counts.clients} "
            f"client_funds={counts.client_funds} transactions={counts.transactions} "
            f"vault={counts.vault} features={counts.features}"
        )
        if counts.client_funds > counts.clients:
            held = counts.client_funds - counts.clients
            print(
                f"transform: {held} clients hold more than one fund; "
                "each is contacted on their largest"
            )

    with SessionLocal() as session:
        resolved = populate_indicators(session, at=args.at)
        print(f"resolve_indicators: resolved {resolved} client(s) as of {args.at.isoformat()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
