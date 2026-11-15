from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.briefing.next_action import (
    LoggedContact,
    NextAction,
    NextActionFacts,
    OpenTouchpoint,
    decide,
)
from app.config import get_settings
from app.db.models.active_clients import (
    CONTACT_INTERACTION_TYPES,
    TOUCHPOINT_OPEN,
    ActiveClientInteraction,
    ActiveClientTouchpoint,
)
from app.db.models.complaints import ClientComplaint
from app.db.models.signals import ClientSituationState
from app.db.models.suppression import Suppression
from app.services.client_touchpoints import today_in_nairobi

MISSABLE_KINDS = ("call", "meeting")

Key = tuple[int, int]


@dataclass(frozen=True)
class ClientNextAction:
    action: NextAction | None
    note: str | None


def _situations(session: Session, keys: Sequence[Key]) -> dict[Key, tuple[str, ...]]:
    rows = session.execute(
        select(
            ClientSituationState.client_id,
            ClientSituationState.unit_fund_id,
            ClientSituationState.situation_code,
        ).where(
            tuple_(ClientSituationState.client_id, ClientSituationState.unit_fund_id).in_(keys),
            ClientSituationState.is_active.is_(True),
        )
    ).all()
    found: dict[Key, list[str]] = {}
    for row in rows:
        found.setdefault((row.client_id, row.unit_fund_id), []).append(row.situation_code)
    return {key: tuple(codes) for key, codes in found.items()}


def _open_touchpoints(
    session: Session, keys: Sequence[Key], today: date
) -> tuple[dict[Key, OpenTouchpoint], dict[Key, OpenTouchpoint]]:
    rows = session.execute(
        select(
            ActiveClientTouchpoint.id,
            ActiveClientTouchpoint.client_id,
            ActiveClientTouchpoint.unit_fund_id,
            ActiveClientTouchpoint.kind,
            ActiveClientTouchpoint.due_date,
        )
        .where(
            tuple_(ActiveClientTouchpoint.client_id, ActiveClientTouchpoint.unit_fund_id).in_(keys),
            ActiveClientTouchpoint.status == TOUCHPOINT_OPEN,
        )
        .order_by(ActiveClientTouchpoint.due_date, ActiveClientTouchpoint.id)
    ).all()
    planned: dict[Key, OpenTouchpoint] = {}
    missed: dict[Key, OpenTouchpoint] = {}
    for row in rows:
        key = (row.client_id, row.unit_fund_id)
        touchpoint = OpenTouchpoint(row.id, row.kind, row.due_date)
        if row.due_date >= today:
            planned.setdefault(key, touchpoint)
        elif row.kind in MISSABLE_KINDS:
            missed[key] = touchpoint
    return planned, missed


def _last_contacts(session: Session, keys: Sequence[Key]) -> dict[Key, LoggedContact]:
    zone = ZoneInfo(get_settings().db_timezone)
    rows = session.execute(
        select(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            ActiveClientInteraction.type,
            ActiveClientInteraction.outcome,
            ActiveClientInteraction.created_at,
        )
        .where(
            tuple_(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id).in_(
                keys
            ),
            ActiveClientInteraction.type.in_(CONTACT_INTERACTION_TYPES),
        )
        .distinct(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id)
        .order_by(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            ActiveClientInteraction.id.desc(),
        )
    ).all()
    return {
        (row.client_id, row.unit_fund_id): LoggedContact(
            row.type, row.outcome, row.created_at.astimezone(zone).date()
        )
        for row in rows
    }


def _clients_who_cannot_be_emailed(session: Session, client_ids: set[int]) -> set[int]:
    with_complaint = session.scalars(
        select(ClientComplaint.client_id).where(
            ClientComplaint.client_id.in_(client_ids), ClientComplaint.status == "open"
        )
    )
    suppressed = session.scalars(
        select(Suppression.client_id).where(Suppression.client_id.in_(client_ids))
    )
    return {*with_complaint, *suppressed}


def next_actions_for(
    session: Session,
    keys: Sequence[Key],
    *,
    situation_reasons: Mapping[Key, str] | None = None,
) -> dict[Key, ClientNextAction]:
    if not keys:
        return {}
    today = today_in_nairobi()
    quiet_days = get_settings().next_action_quiet_days
    situations = _situations(session, keys)
    planned, missed = _open_touchpoints(session, keys, today)
    contacts = _last_contacts(session, keys)
    blocked = _clients_who_cannot_be_emailed(session, {client_id for client_id, _ in keys})
    reasons = situation_reasons or {}

    results: dict[Key, ClientNextAction] = {}
    for key in keys:
        facts = NextActionFacts(
            situations=situations.get(key, ()),
            planned=planned.get(key),
            missed=missed.get(key),
            last_contact=contacts.get(key),
            can_email=key[0] not in blocked,
        )
        decision = decide(facts, today, quiet_days, reasons.get(key))
        results[key] = ClientNextAction(decision.action, decision.note)
    return results


def next_action_for_client(session: Session, client_id: int, unit_fund_id: int) -> ClientNextAction:
    key = (client_id, unit_fund_id)
    return next_actions_for(session, [key])[key]
