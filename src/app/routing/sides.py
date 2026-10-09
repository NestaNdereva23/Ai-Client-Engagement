from __future__ import annotations

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.db.bulk import copy_upsert

logger = structlog.get_logger(__name__)

CAPITAL_PRESERVATION_KES = 6_000.0

SIDE_ACTIVE = "active"
SIDE_INACTIVE = "inactive"

_CLIENT_BALANCE = """
    SELECT COALESCE(a.client_id, i.client_id) AS client_id,
           CASE
               WHEN a.client_id IS NOT NULL THEN COALESCE(a.total, 0)
               ELSE COALESCE(i.total, 0)
           END AS total_balance,
           a.client_id IS NOT NULL AS in_active,
           i.client_id IS NOT NULL AS in_inactive
    FROM (
        SELECT client_id, SUM(balance) AS total FROM active_client_fund GROUP BY client_id
    ) a
    FULL OUTER JOIN (
        SELECT client_id, SUM(balance) AS total FROM client_fund GROUP BY client_id
    ) i ON a.client_id = i.client_id
"""

_BALANCE_BY_CLIENT = text(_CLIENT_BALANCE)

_SET_CLAUSE = (
    "side = EXCLUDED.side, "
    "balance = EXCLUDED.balance, "
    "threshold_kes = EXCLUDED.threshold_kes, "
    "source_feed = EXCLUDED.source_feed, "
    "decided_at = now()"
)

_ACTIVE_CLIENT_IDS = text(
    f"SELECT client_id FROM ({_CLIENT_BALANCE}) totals WHERE total_balance >= :threshold"
).bindparams(bindparam("threshold"))


def active_client_ids(session: Session, *, threshold: float = CAPITAL_PRESERVATION_KES) -> set[int]:
    return set(session.scalars(_ACTIVE_CLIENT_IDS, {"threshold": threshold}))


def _source_feed(in_active: bool, in_inactive: bool) -> str:
    if in_active and in_inactive:
        return "both"
    if in_active:
        return SIDE_ACTIVE
    return SIDE_INACTIVE


def assign_sides(
    session: Session, *, threshold: float = CAPITAL_PRESERVATION_KES
) -> dict[str, int]:
    rows_in = session.execute(_BALANCE_BY_CLIENT).all()

    counts = {SIDE_ACTIVE: 0, SIDE_INACTIVE: 0}
    rows: list[dict[str, object]] = []
    for client_id, total_balance, in_active, in_inactive in rows_in:
        balance = float(total_balance)
        side = SIDE_INACTIVE if balance < threshold else SIDE_ACTIVE
        counts[side] += 1
        rows.append(
            {
                "client_id": client_id,
                "side": side,
                "balance": balance,
                "threshold_kes": threshold,
                "source_feed": _source_feed(in_active, in_inactive),
            }
        )

    copy_upsert(session, "client_side", rows, ["client_id"], _SET_CLAUSE)
    session.commit()
    logger.info(
        "routing.sides_assigned",
        active=counts[SIDE_ACTIVE],
        inactive=counts[SIDE_INACTIVE],
        threshold=threshold,
    )
    return counts
