"""Command line entry point for an ingestion run.

Run a fresh pull:
    uv run python scripts/ingest.py

Resume a run that stopped:
    uv run python scripts/ingest.py --run-id <id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.ingestion.api_client import CytonnClient  # noqa: E402
from app.ingestion.endpoints import resolve_endpoint  # noqa: E402
from app.workers.ingestion import IngestionAborted, IngestionWorker  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull the endpoint into raw staging.")
    parser.add_argument("--run-id", help="Resume this run instead of starting a new one.")
    parser.add_argument(
        "--endpoint",
        default="inactive-clients",
        help="Label and path for the endpoint (default: inactive-clients).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Stop after this many pages (safety cap).",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.cytonn_api_base_url or not settings.cytonn_api_key:
        parser.error("CY_API_BASE_URL and CY_API_KEY must be set in the environment.")

    config = resolve_endpoint(args.endpoint, settings)
    client = CytonnClient(settings.cytonn_api_base_url, settings.cytonn_api_key)
    worker = IngestionWorker(
        client,
        endpoint=args.endpoint,
        fetch_path=config.fetch_path,
        max_pages=args.max_pages,
        fund_model=config.fund_model,
        client_model=config.client_model,
        schema_drift_fn=config.schema_drift_fn,
        count_field=config.count_field,
    )

    try:
        result = worker.run(run_id=args.run_id)
    except IngestionAborted as exc:
        print(f"Ingestion aborted: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    print(
        f"Run {result.run_id} {result.state}: "
        f"{result.pages} page(s), {result.records_seen} seen, "
        f"{result.records_written} written, {result.records_rejected} rejected, "
        f"shortfall {result.shortfall}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
