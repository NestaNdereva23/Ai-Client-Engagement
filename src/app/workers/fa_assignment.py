"""Work out tonight's advisor allocation and write it to fa_assignment.

The decision itself lives in risk/fa_allocation.py, which touches no
database. This is the thin layer around it: read who owns whom today, call
the allocation, write the answer back, audit what changed.

The advisor is chosen once per client and then written onto every one of
that client's rows, so the table stays keyed on (client_id, unit_fund_id)
while a person can never be split across two advisors.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import FaRecord
from app.db.models.fa_assignment import FaAssignment
from app.risk.fa_allocation import AdvisorAllocation, ClientLoad, allocate_advisors
from app.transform.load import upsert

SOURCE = "roster"

_UPDATE_COLUMNS = ["fa_id", "fa_name", "source"]


def allocate_and_persist(
    session: Session,
    run_id: str,
    *,
    roster: Sequence[FaRecord],
    clients: Sequence[ClientLoad],
    keys: Sequence[tuple[int, int]],
) -> AdvisorAllocation:
    """Assign this run's clients to advisors and store the result.

    keys are the (client_id, unit_fund_id) pairs the run covers; every one
    whose client got an owner is written. An empty roster does nothing at
    all and returns an empty allocation, leaving whatever is already in the
    table alone.
    """
    if not roster or not clients:
        return AdvisorAllocation()

    client_ids = sorted({client.client_id for client in clients})
    current_owners = {
        client_id: fa_id
        for client_id, fa_id in session.execute(
            select(FaAssignment.client_id, FaAssignment.fa_id).where(
                FaAssignment.client_id.in_(client_ids), FaAssignment.fa_id.is_not(None)
            )
        ).all()
    }

    allocation = allocate_advisors(roster, clients, current_owners)

    names = {record.fa_id: record.name for record in roster}
    rows: list[dict[str, Any]] = []
    for client_id, unit_fund_id in keys:
        fa_id = allocation.owner.get(client_id)
        if fa_id is None:
            continue
        rows.append(
            {
                "client_id": client_id,
                "unit_fund_id": unit_fund_id,
                "fa_id": fa_id,
                "fa_name": names[fa_id],
                "source": SOURCE,
            }
        )
    upsert(
        session,
        FaAssignment,
        rows,
        ("client_id", "unit_fund_id"),
        _UPDATE_COLUMNS,
        extra_set={"updated_at": func.now()},
    )

    first_time = sum(1 for client_id in allocation.owner if client_id not in current_owners)
    moved = sum(
        1
        for client_id, fa_id in allocation.owner.items()
        if client_id in current_owners and current_owners[client_id] != fa_id
    )
    record_audit(
        session,
        entity_type="fa_assignment",
        action="allocate",
        entity_id=run_id,
        run_id=run_id,
        detail={
            "advisors": len(roster),
            "clients": len(allocation.owner),
            "rows_written": len(rows),
            "first_time": first_time,
            "reassigned": moved,
            "lent_for_tonight": len(allocation.covering),
        },
    )
    return allocation


__all__ = ["allocate_and_persist", "SOURCE"]
