from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.routing.sides import assign_sides  # noqa: E402


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    with SessionLocal() as session:
        counts = assign_sides(session)
    print(f"sides: active={counts['active']} inactive={counts['inactive']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
