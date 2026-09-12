"""One-off backfill: seed active_configuration from what is live today.

Run once, right after the prompt-config-versioning-foundation migration:

    uv run python scripts/inactive/backfill_active_configuration.py

Reads today's active message_angle_catalog version (per angle), active
tier_contract version (per tier), and active business_rules version, and
writes each into active_configuration. Safe to run again later: it always
overwrites with today's currently active versions, never with stale ones.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rules import versioning  # noqa: E402
from app.rules.catalog import load_active_angles  # noqa: E402
from app.rules.store import load_active_rules  # noqa: E402
from app.rules.tier_contract import load_active_tiers  # noqa: E402
from app.rules.versioning import DEFAULT_COMPONENT_KEY  # noqa: E402


def main() -> int:
    configure_logging(get_settings().log_level)
    today = date.today()

    with SessionLocal() as session:
        angles = load_active_angles(session, today)
        for angle, row in angles.items():
            versioning.record_published_version(
                session, "message_angle_catalog", angle, row.version
            )
        print(f"message_angle_catalog: seeded {len(angles)} angle(s)")

        tiers = load_active_tiers(session, today)
        for tier, row in tiers.items():
            versioning.record_published_version(session, "tier_contract", tier, row.version)
        print(f"tier_contract: seeded {len(tiers)} tier(s)")

        rules = load_active_rules(session, today)
        if rules:
            versioning.record_published_version(
                session, "business_rules", DEFAULT_COMPONENT_KEY, rules[0].version
            )
            print(f"business_rules: seeded version {rules[0].version}")
        else:
            print("business_rules: no active version found, nothing seeded")

        session.commit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
