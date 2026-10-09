from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.agents.action_catalog import (  # noqa: E402
    ActionCatalogValidationError,
    set_action_content_mix,
)
from app.agents.guide_mix import MIX_LABELS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Change how much one action explains and how much it asks."
    )
    parser.add_argument("--action", required=True, help="The action code, for example fee_warning.")
    parser.add_argument("--mix", required=True, choices=sorted(MIX_LABELS))
    parser.add_argument("--by", required=True, help="Who is making the change.")
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        try:
            version = set_action_content_mix(session, args.action, args.mix, changed_by=args.by)
        except ActionCatalogValidationError as exc:
            print(f"not changed: {exc}")
            return 1
        session.commit()

    print(f"{args.action} is now {MIX_LABELS[args.mix]} (catalogue version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
