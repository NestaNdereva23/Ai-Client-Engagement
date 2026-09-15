from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.models import PiiVault  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.transform.load import normalize_phone  # noqa: E402

_BATCH_SIZE = 1000


def run(*, apply: bool) -> dict[str, int]:
    updated = 0
    unresolvable = 0
    scanned = 0

    with SessionLocal() as session:
        rows = session.scalars(
            select(PiiVault).where(
                PiiVault.contact_phone.is_(None), PiiVault.contact_whatsapp.isnot(None)
            )
        )
        for vault in rows:
            scanned += 1
            phone = normalize_phone(vault.contact_whatsapp)
            if phone is None:
                unresolvable += 1
                continue
            if apply:
                vault.contact_phone = phone
            updated += 1
            if apply and updated % _BATCH_SIZE == 0:
                session.commit()
        if apply:
            session.commit()

    return {"scanned": scanned, "updated": updated, "unresolvable": unresolvable}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    counts = run(apply=args.apply)
    print(counts)
    if not args.apply:
        print("dry run, no rows written; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
