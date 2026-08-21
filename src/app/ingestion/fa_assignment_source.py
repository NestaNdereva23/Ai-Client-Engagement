"""The FA-assignment source: a contract fixed now so a real source can swap
in later with no change to whoever calls it.

The active-clients feed carries no FA (relationship-manager) field, so there
is nothing to read one from. Two implementations:

StubFaAssignmentSource returns fa_id=None for every client-fund it is asked
about, and the digest builder treats a null fa_id as "group by fund
instead". DbFaAssignmentSource reads fa_assignment, which the nightly
allocation fills in from the roster seeded in the environment.

Every caller downstream depends only on the FaAssignmentSource protocol, so
the day Cytonn does supply a real FA field, a new class implementing
fetch_assignments and one line in app/config.py is the whole change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.fa_assignment import FaAssignment


class FaAssignmentRecord(BaseModel):
    """One client-fund's assignment to an account manager.

    fa_id is null for every row the stub returns; fa_name is display only
    and never enters a model prompt.
    """

    client_id: int
    unit_fund_id: int
    fa_id: str | None = None
    fa_name: str | None = None
    source: str  # "stub" today; a real source name once one exists


@runtime_checkable
class FaAssignmentSource(Protocol):
    def fetch_assignments(self, client_ids: Sequence[int]) -> list[FaAssignmentRecord]: ...


class StubFaAssignmentSource:
    """Returns fa_id=None for every client-fund relationship, matching the
    notebook's own stand-in: there is no FA-assignment field on the
    active-clients feed to read yet.

    Which relationships to answer for comes from active_client_fund, the
    same active-book table the risk engine reads, so a caller only has to
    name client ids -- no separate fixture or mapping to keep in sync.
    Without a session (the default), it answers every call with an empty
    list, which is the same honest "no assignment" the notebook's stand-in
    gives.
    """

    def __init__(self, session: Session | None = None) -> None:
        self._session = session

    def fetch_assignments(self, client_ids: Sequence[int]) -> list[FaAssignmentRecord]:
        if self._session is None or not client_ids:
            return []
        rows = self._session.execute(
            select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id).where(
                ActiveClientFund.client_id.in_(client_ids)
            )
        ).all()
        return [
            FaAssignmentRecord(
                client_id=client_id,
                unit_fund_id=unit_fund_id,
                fa_id=None,
                fa_name=None,
                source="stub",
            )
            for client_id, unit_fund_id in rows
        ]


class DbFaAssignmentSource:
    """Returns whatever fa_assignment already holds for the clients asked
    about, and nothing else.

    Reading is deliberately separate from deciding: the nightly allocation
    in risk/fa_allocation.py works out who owns whom and writes the rows,
    this only reads them back. Keeping the two apart means each can be
    tested on its own, and the digest never depends on an allocation having
    just run. A client-fund with no row here simply comes back absent, and
    the digest falls back to grouping it by fund, exactly as the stub makes
    it do everywhere.
    """

    def __init__(self, session: Session | None = None) -> None:
        self._session = session

    def fetch_assignments(self, client_ids: Sequence[int]) -> list[FaAssignmentRecord]:
        if self._session is None or not client_ids:
            return []
        rows = self._session.scalars(
            select(FaAssignment).where(FaAssignment.client_id.in_(client_ids))
        )
        return [
            FaAssignmentRecord(
                client_id=row.client_id,
                unit_fund_id=row.unit_fund_id,
                fa_id=row.fa_id,
                fa_name=row.fa_name,
                source=row.source,
            )
            for row in rows
        ]


def get_fa_assignment_source(
    session: Session | None = None, settings: Settings | None = None
) -> FaAssignmentSource:
    """Build the configured FaAssignmentSource. The one place an
    implementation is chosen; downstream code only ever depends on the
    protocol. With nothing configured, a seeded roster selects the database
    source and no roster selects the stub.
    """
    settings = settings or get_settings()
    source = settings.fa_assignment_source or ("db" if settings.fa_records else "stub")
    if source == "stub":
        return StubFaAssignmentSource(session=session)
    if source == "db":
        return DbFaAssignmentSource(session=session)
    raise ValueError(f"unknown FA-assignment source: {source!r}")
