from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db.models.fa_assignment import FaAssignment  # noqa: E402
from app.db.models.risk import ClientRiskFeatures  # noqa: E402
from app.db.models.routing import ClientSide, ClientTier  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.routing.sides import SIDE_INACTIVE  # noqa: E402

TABLES = (ClientTier, ClientRiskFeatures, FaAssignment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete active-side rows that belong to clients now on the inactive side."
    )
    parser.add_argument("--apply", action="store_true", help="Delete. Without it, only count.")
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        inactive = select(ClientSide.client_id).where(ClientSide.side == SIDE_INACTIVE)
        for model in TABLES:
            stale = session.scalar(
                select(func.count()).select_from(model).where(model.client_id.in_(inactive))
            )
            total = session.scalar(select(func.count()).select_from(model))
            print(f"{model.__tablename__}: {stale} of {total} rows belong to inactive-side clients")
            if args.apply:
                session.execute(delete(model).where(model.client_id.in_(inactive)))
        if args.apply:
            session.commit()
            for model in TABLES:
                remaining = session.scalar(select(func.count()).select_from(model))
                print(f"{model.__tablename__}: {remaining} rows left")
        else:
            session.rollback()
            print("Dry run. Pass --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
