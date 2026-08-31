from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.workers.batch_ingest import run_batch_ingest_tick  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    tracer = get_tracer(settings)

    try:
        with SessionLocal() as session:
            result = run_batch_ingest_tick(session, settings=settings, tracer=tracer)
    finally:
        tracer.shutdown()

    print(
        f"{result.considered} batch(es) in flight: {result.ingested} ingested, "
        f"{result.still_in_progress} still in progress, {result.failed} failed, "
        f"{result.stale} stale (left for a person)"
    )
    for batch in result.batches:
        print(
            f"  {batch.generation_batch_id} (campaign_id={batch.campaign_id}): "
            f"{batch.status}, accepted={batch.accepted}, rejected={batch.rejected}"
        )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
