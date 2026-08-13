"""Command line entry point for one nightly risk detection run.

Run a fresh pass:
    uv run python scripts/risk/detect.py

Resume a run that stopped:
    uv run python scripts/risk/detect.py --run-id <id>

Runs the whole pipeline end to end: ingest the active-clients feed, fetch
complaints and FA assignments, transform, score, route, and write the
snapshot -- matching how scripts/active/ingest.py invokes the plain
ingestion worker.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.ingestion.api_client import CytonnClient  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.workers.ingestion import IngestionAborted  # noqa: E402
from app.workers.risk_detection import RiskDetectionWorker, RiskRunAborted  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the nightly risk detection pipeline.")
    parser.add_argument("--run-id", help="Resume this run instead of starting a new one.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Stop the ingest stage after this many pages (safety cap).",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.cytonn_api_base_url or not settings.cytonn_api_key:
        parser.error("CY_API_BASE_URL and CY_API_KEY must be set in the environment.")

    client = CytonnClient(settings.cytonn_api_base_url, settings.cytonn_api_key)
    worker = RiskDetectionWorker(client, max_pages=args.max_pages)

    try:
        result = worker.run(run_id=args.run_id)
    except (IngestionAborted, RiskRunAborted) as exc:
        print(f"Risk detection aborted: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    print(
        f"Run {result.run_id} {result.state}: {result.clients_seen} client-fund(s) seen, "
        f"routes_changed={result.routes_changed}."
    )
    print(f"route_distribution={result.route_distribution}")
    print(f"signals_fired={result.signals_fired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
