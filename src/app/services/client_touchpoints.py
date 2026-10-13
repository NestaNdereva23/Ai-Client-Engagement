from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.active_clients import (
    CHANNEL_BY_INTERACTION_TYPE,
    TOUCHPOINT_DONE,
    TOUCHPOINT_OPEN,
    TOUCHPOINT_REMOVED,
    ActiveClientFund,
    ActiveClientTouchpoint,
)
from app.db.models.fa_assignment import FaAssignment

ENTITY_TYPE = "active_client_touchpoint"
REMINDER_KIND = "reminder"
FOLLOW_UP_KIND_BY_CHANNEL = {"call": "call", "meeting": "meeting", "email": "email"}


class TouchpointClientNotFound(LookupError):
    pass


class NotTheOwner(PermissionError):
    pass


class TouchpointNotFound(LookupError):
    pass


class TouchpointDateInPast(ValueError):
    pass


def today_in_nairobi() -> date:
    return datetime.now(ZoneInfo(get_settings().db_timezone)).date()


def _entity_id(client_id: int, unit_fund_id: int) -> str:
    return f"{client_id}/{unit_fund_id}"


def require_client_fund(session: Session, client_id: int, unit_fund_id: int) -> None:
    if session.get(ActiveClientFund, (client_id, unit_fund_id)) is None:
        raise TouchpointClientNotFound(_entity_id(client_id, unit_fund_id))


def require_owner(session: Session, client_id: int, unit_fund_id: int, username: str) -> None:
    require_client_fund(session, client_id, unit_fund_id)
    owner = session.scalar(
        select(FaAssignment.fa_id).where(
            FaAssignment.client_id == client_id, FaAssignment.unit_fund_id == unit_fund_id
        )
    )
    if owner != username:
        raise NotTheOwner(_entity_id(client_id, unit_fund_id))


def list_open_touchpoints(
    session: Session, client_id: int, unit_fund_id: int
) -> list[ActiveClientTouchpoint]:
    return list(
        session.scalars(
            select(ActiveClientTouchpoint)
            .where(
                ActiveClientTouchpoint.client_id == client_id,
                ActiveClientTouchpoint.unit_fund_id == unit_fund_id,
                ActiveClientTouchpoint.status == TOUCHPOINT_OPEN,
            )
            .order_by(
                ActiveClientTouchpoint.due_date,
                ActiveClientTouchpoint.due_time.nullslast(),
                ActiveClientTouchpoint.id,
            )
        )
    )


def require_not_in_past(due_date: date) -> None:
    if due_date < today_in_nairobi():
        raise TouchpointDateInPast(due_date.isoformat())


def _stage_touchpoint(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    kind: str,
    title: str,
    due_date: date,
    due_time: time | None,
    minutes: int | None,
    created_by: str,
) -> ActiveClientTouchpoint:
    require_not_in_past(due_date)
    row = ActiveClientTouchpoint(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        kind=kind,
        title=title,
        due_date=due_date,
        due_time=due_time,
        minutes=minutes,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        entity_type=ENTITY_TYPE,
        action="create",
        entity_id=_entity_id(client_id, unit_fund_id),
        actor_id=created_by,
        detail={"touchpoint_id": row.id, "kind": kind, "due_date": due_date.isoformat()},
    )
    return row


def add_touchpoint(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    username: str,
    kind: str,
    title: str,
    due_date: date,
    due_time: time | None,
    minutes: int | None,
) -> ActiveClientTouchpoint:
    require_owner(session, client_id, unit_fund_id, username)
    row = _stage_touchpoint(
        session,
        client_id,
        unit_fund_id,
        kind=kind,
        title=title,
        due_date=due_date,
        due_time=due_time,
        minutes=minutes,
        created_by=username,
    )
    session.commit()
    return row


def remove_touchpoint(
    session: Session, client_id: int, unit_fund_id: int, touchpoint_id: int, *, username: str
) -> None:
    require_owner(session, client_id, unit_fund_id, username)
    removed = session.execute(
        update(ActiveClientTouchpoint)
        .where(
            ActiveClientTouchpoint.id == touchpoint_id,
            ActiveClientTouchpoint.client_id == client_id,
            ActiveClientTouchpoint.unit_fund_id == unit_fund_id,
            ActiveClientTouchpoint.status == TOUCHPOINT_OPEN,
        )
        .values(status=TOUCHPOINT_REMOVED)
    )
    if removed.rowcount == 0:
        raise TouchpointNotFound(str(touchpoint_id))
    record_audit(
        session,
        entity_type=ENTITY_TYPE,
        action="remove",
        entity_id=_entity_id(client_id, unit_fund_id),
        actor_id=username,
        detail={"touchpoint_id": touchpoint_id},
    )
    session.commit()


def stage_snooze_reminder(
    session: Session, client_id: int, unit_fund_id: int, *, due_date: date, created_by: str
) -> None:
    _stage_touchpoint(
        session,
        client_id,
        unit_fund_id,
        kind=REMINDER_KIND,
        title="Snooze ends",
        due_date=due_date,
        due_time=None,
        minutes=None,
        created_by=created_by,
    )


def stage_follow_up(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    interaction_type: str,
    due_date: date,
    created_by: str,
) -> None:
    channel = CHANNEL_BY_INTERACTION_TYPE[interaction_type]
    _stage_touchpoint(
        session,
        client_id,
        unit_fund_id,
        kind=FOLLOW_UP_KIND_BY_CHANNEL.get(channel, REMINDER_KIND),
        title=f"Follow up by {channel}",
        due_date=due_date,
        due_time=None,
        minutes=None,
        created_by=created_by,
    )


def check_touchpoint_is_open(
    session: Session, client_id: int, unit_fund_id: int, touchpoint_id: int
) -> None:
    found = session.scalar(
        select(ActiveClientTouchpoint.id).where(
            ActiveClientTouchpoint.id == touchpoint_id,
            ActiveClientTouchpoint.client_id == client_id,
            ActiveClientTouchpoint.unit_fund_id == unit_fund_id,
            ActiveClientTouchpoint.status == TOUCHPOINT_OPEN,
        )
    )
    if found is None:
        raise TouchpointNotFound(str(touchpoint_id))


def mark_touchpoint_done(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    touchpoint_id: int,
    *,
    interaction_id: int,
    actor_id: str,
) -> None:
    session.execute(
        update(ActiveClientTouchpoint)
        .where(ActiveClientTouchpoint.id == touchpoint_id)
        .values(status=TOUCHPOINT_DONE, done_by_interaction_id=interaction_id)
    )
    record_audit(
        session,
        entity_type=ENTITY_TYPE,
        action="done",
        entity_id=_entity_id(client_id, unit_fund_id),
        actor_id=actor_id,
        detail={"touchpoint_id": touchpoint_id, "interaction_id": interaction_id},
    )
