from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rag.ingest import ingest_report_pdf  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a weekly report PDF for RAG retrieval.")
    parser.add_argument("--path", required=True, help="Path to the report PDF.")
    parser.add_argument("--title", default="Cytonn Weekly Report")
    parser.add_argument("--source", default="cytonn-weekly")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    with SessionLocal() as session:
        result = ingest_report_pdf(
            session, args.path, document_title=args.title, document_source=args.source
        )

    action = "created" if result.created else "refreshed"
    print(
        f"{action} version {result.version_no} (version_id={result.version_id}) "
        f"with {result.chunks} chunk(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
