from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.db.models.agent_run import AgentRun
from app.db.models.fa_assignment import FaAssignment

_scope: ContextVar[str | None] = ContextVar("agent_fa_scope", default=None)


def current_fa_id() -> str | None:
    return _scope.get()


@contextmanager
def fa_scope(fa_id: str | None) -> Iterator[None]:
    token = _scope.set(fa_id)
    try:
        yield
    finally:
        _scope.reset(token)


def narrow_to_scope(
    statement: Select,
    client_id: InstrumentedAttribute,
    unit_fund_id: InstrumentedAttribute,
) -> Select:
    fa_id = current_fa_id()
    if fa_id is None:
        return statement
    owned = select(FaAssignment.client_id, FaAssignment.unit_fund_id).where(
        FaAssignment.fa_id == fa_id
    )
    return statement.where(tuple_(client_id, unit_fund_id).in_(owned))


def fa_id_of_run(session: Session, run_id: int | None) -> str | None:
    if run_id is None:
        return None
    run = session.get(AgentRun, run_id)
    return None if run is None else run.fa_id


def advisors_with_clients(session: Session) -> list[str]:
    rows = session.scalars(
        select(FaAssignment.fa_id)
        .where(FaAssignment.fa_id.is_not(None), FaAssignment.fa_id != "")
        .distinct()
        .order_by(FaAssignment.fa_id)
    )
    return list(rows)
