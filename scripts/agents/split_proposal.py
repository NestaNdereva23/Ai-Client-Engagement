from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.agents.proposal_split import (  # noqa: E402
    ANGLE_TEST,
    GUIDE_TEST,
    SIDES,
    SplitRefused,
    split_proposal,
)
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split one proposal into two versions that go to half the clients each."
    )
    parser.add_argument("--proposal", type=int, required=True, help="The proposal id.")
    parser.add_argument(
        "--test",
        required=True,
        choices=(ANGLE_TEST, GUIDE_TEST),
        help="angle: two angles. guide: the same angle with and without a client guide.",
    )
    parser.add_argument("--other-angle", help="The second angle. Only for an angle test.")
    parser.add_argument("--by", required=True, help="Who is making the split.")
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        try:
            split = split_proposal(
                session,
                args.proposal,
                test=args.test,
                other_angle=args.other_angle,
                split_by=args.by,
            )
        except SplitRefused as exc:
            print(f"not split: {exc}")
            return 1
        session.commit()

    for variant in SIDES:
        side = split.sides[variant]
        clients = sum(1 for assigned in split.variant_of.values() if assigned == variant)
        guide = side.content_mix or "no client guide"
        print(f"version {variant}: {clients} clients, angle {side.angle}, guide mix {guide}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
