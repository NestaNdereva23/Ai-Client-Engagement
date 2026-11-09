from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.campaigns.completion import complete_finished_campaigns  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def main() -> int:
    with SessionLocal() as session:
        done = complete_finished_campaigns(session)
        session.commit()
    print(f"completed campaigns: {done or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
