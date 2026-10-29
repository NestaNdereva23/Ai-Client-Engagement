from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.query_banding import band_label
from app.campaigns.eligibility import check_eligibility
from app.campaigns.scheduler import DEFAULT_BATCH_LIMIT, select_due_enrollments
from app.campaigns.state_machine import transition_enrollment
from app.config import get_settings
from app.db.models.active_clients import ActiveTransaction
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog, TouchPlan
from app.db.models.models import Transactions
from app.db.models.outreach import OutreachMessage
from app.db.models.routing import ClientSide
from app.transform.features import VALUE_BAND_CUTOFFS

FIRST_TOUCH = "first_touch"
FOLLOW_UP = "follow_up"
EXCERPT_LIMIT = 240
NOTE_LIMIT = 300
MAX_RECENT_EVENTS = 10
DEPOSIT_TXN_TYPE = "purchase"
OPTED_OUT = "opted_out"
DEPOSITED = "deposited"
ALREADY_PLANNED = "already_planned"
NOT_SENT = "no_draft"
BALANCE_BANDS_KES = (1.0, 50.0, 600.0, 6_000.0, 50_000.0)


@dataclass(frozen=True)
class EarlierTouch:
    step_no: int
    angle: str | None
    subject: str | None
    excerpt: str | None
    send_status: str
    delivery_status: str | None
    days_since_sent: int | None
    opened: bool
    replied: bool
    bounced: bool


@dataclass(frozen=True)
class RecentEvent:
    type: str
    days_ago: int


@dataclass(frozen=True)
class SendSnapshot:
    step_no: int
    side: str | None
    balance_band: str | None
    balance_since: str | None


@dataclass(frozen=True)
class SideChange:
    side_now: str | None
    balance_band_now: str | None
    at_send: tuple[SendSnapshot, ...]


@dataclass(frozen=True)
class SlotContext:
    enrollment_id: int
    step_no: int
    kind: str
    earlier_touches: tuple[EarlierTouch, ...] = ()
    events_since_last_touch: tuple[RecentEvent, ...] = ()
    deposit_since_last_touch: str | None = None
    next_time_note: str | None = None
    side_change: SideChange | None = None
    skip_reason: str | None = None

    @property
    def prior_angle(self) -> str | None:
        return self.earlier_touches[-1].angle if self.earlier_touches else None

    @property
    def missing_history(self) -> bool:
        return len(self.earlier_touches) < self.step_no - 1


@dataclass(frozen=True)
class CampaignSlots:
    ready: tuple[SlotContext, ...]
    dropped: tuple[SlotContext, ...]

    def dropped_reasons(self) -> dict[str, int]:
        return dict(Counter(slot.skip_reason for slot in self.dropped))


def build_campaign_slots(
    session: Session,
    campaign_id: int,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    now: datetime | None = None,
) -> CampaignSlots:
    now = now or datetime.now(UTC)
    contexts = [
        _slot_context(session, enrollment, now)
        for enrollment in select_due_enrollments(session, campaign_id=campaign_id, limit=limit)
    ]
    return CampaignSlots(
        ready=tuple(slot for slot in contexts if slot.skip_reason is None),
        dropped=tuple(slot for slot in contexts if slot.skip_reason is not None),
    )


def _slot_context(session: Session, enrollment: Enrollment, now: datetime) -> SlotContext:
    step_no = enrollment.current_step + 1
    kind = FIRST_TOUCH if step_no == 1 else FOLLOW_UP
    verdict = check_eligibility(session, enrollment)
    if not verdict.eligible:
        return SlotContext(enrollment.enrollment_id, step_no, kind, skip_reason=verdict.reason)

    if _has_plan(session, enrollment, step_no):
        return SlotContext(enrollment.enrollment_id, step_no, kind, skip_reason=ALREADY_PLANNED)

    rows = _touch_rows(session, enrollment)
    events = _client_events(session, enrollment.client_id)
    sent = [row.sent_at for row in rows if row.sent_at is not None]
    since = max(sent) if sent else enrollment.enrolled_at

    if any(event.type == "optout" for event in events):
        return _stopped(session, enrollment, step_no, kind, OPTED_OUT, "stopped_optout")
    band = _deposit_band_since(session, enrollment.client_id, since)
    if band is not None:
        return _stopped(
            session, enrollment, step_no, kind, DEPOSITED, "stopped_reengaged", deposit=band
        )

    return SlotContext(
        enrollment_id=enrollment.enrollment_id,
        step_no=step_no,
        kind=kind,
        earlier_touches=_earlier_touches(rows, events, now),
        events_since_last_touch=_recent_events(events, since, now),
        next_time_note=_last_note(session, enrollment, step_no),
        side_change=_side_change(session, enrollment.client_id, rows),
    )


def _has_plan(session: Session, enrollment: Enrollment, step_no: int) -> bool:
    return (
        session.scalar(
            select(TouchPlan.plan_id).where(
                TouchPlan.enrollment_id == enrollment.enrollment_id, TouchPlan.step_no == step_no
            )
        )
        is not None
    )


def _stopped(
    session: Session,
    enrollment: Enrollment,
    step_no: int,
    kind: str,
    reason: str,
    status: str,
    *,
    deposit: str | None = None,
) -> SlotContext:
    to_status = "excluded" if enrollment.current_step == 0 else status
    transition_enrollment(session, enrollment, to_status=to_status, reason=reason)
    return SlotContext(
        enrollment.enrollment_id,
        step_no,
        kind,
        deposit_since_last_touch=deposit,
        skip_reason=reason,
    )


def _touch_rows(session: Session, enrollment: Enrollment):
    angle = func.coalesce(TouchPlan.angle, CampaignStep.message_angle)
    return session.execute(
        select(
            TouchLog.step_no,
            TouchLog.sent_at,
            TouchLog.delivery_status,
            TouchLog.side_at_send,
            TouchLog.balance_at_send,
            OutreachMessage.ai_draft_content.label("draft"),
            OutreachMessage.status.label("message_status"),
            angle.label("angle"),
        )
        .outerjoin(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
        .outerjoin(
            TouchPlan,
            (TouchPlan.enrollment_id == TouchLog.enrollment_id)
            & (TouchPlan.step_no == TouchLog.step_no),
        )
        .outerjoin(
            CampaignStep,
            (CampaignStep.campaign_id == enrollment.campaign_id)
            & (CampaignStep.step_no == TouchLog.step_no),
        )
        .where(TouchLog.enrollment_id == enrollment.enrollment_id)
        .order_by(TouchLog.step_no)
    ).all()


def _client_events(session: Session, client_id: int) -> list[ContactEvent]:
    return list(
        session.scalars(
            select(ContactEvent)
            .where(ContactEvent.client_id == client_id)
            .order_by(ContactEvent.occurred_at)
        )
    )


def _days_since(moment: datetime, now: datetime) -> int:
    return max((now - moment).days, 0)


def _earlier_touches(rows, events: list[ContactEvent], now: datetime) -> tuple[EarlierTouch, ...]:
    touches = []
    for index, row in enumerate(rows):
        later = rows[index + 1].sent_at if index + 1 < len(rows) else None
        seen = {
            event.type
            for event in events
            if row.sent_at is not None
            and event.occurred_at >= row.sent_at
            and (later is None or event.occurred_at < later)
        }
        draft = row.draft or {}
        body = str(draft.get("body") or "")[:EXCERPT_LIMIT]
        touches.append(
            EarlierTouch(
                step_no=row.step_no,
                angle=row.angle,
                subject=draft.get("subject"),
                excerpt=body or None,
                send_status="sent" if row.sent_at else (row.message_status or NOT_SENT),
                delivery_status=row.delivery_status,
                days_since_sent=_days_since(row.sent_at, now) if row.sent_at else None,
                opened="open" in seen,
                replied="reply" in seen,
                bounced="bounce" in seen,
            )
        )
    return tuple(touches)


def _recent_events(
    events: list[ContactEvent], since: datetime, now: datetime
) -> tuple[RecentEvent, ...]:
    recent = [event for event in events if event.occurred_at >= since]
    newest_first = sorted(recent, key=lambda event: event.occurred_at, reverse=True)
    return tuple(
        RecentEvent(event.type, _days_since(event.occurred_at, now))
        for event in newest_first[:MAX_RECENT_EVENTS]
    )


def _last_note(session: Session, enrollment: Enrollment, step_no: int) -> str | None:
    note = session.scalar(
        select(TouchPlan.next_time_note)
        .where(TouchPlan.enrollment_id == enrollment.enrollment_id, TouchPlan.step_no < step_no)
        .order_by(TouchPlan.step_no.desc())
        .limit(1)
    )
    return note[:NOTE_LIMIT] if note else None


def _deposit_band_since(session: Session, client_id: int, since: datetime) -> str | None:
    day = since.astimezone(ZoneInfo(get_settings().db_timezone)).date()
    total = 0.0
    for table in (Transactions, ActiveTransaction):
        total += session.scalar(
            select(func.coalesce(func.sum(table.amount), 0.0)).where(
                table.client_id == client_id,
                table.txn_type == DEPOSIT_TXN_TYPE,
                table.txn_date >= day,
            )
        )
    return band_label(VALUE_BAND_CUTOFFS, total) if total > 0 else None


def _balance_band(balance: float | None) -> str | None:
    return None if balance is None else band_label(BALANCE_BANDS_KES, balance)


def _balance_since(then: float | None, now: float | None) -> str | None:
    if then is None or now is None:
        return None
    return "up" if now > then else "down" if now < then else "same"


def _side_change(session: Session, client_id: int, rows) -> SideChange:
    today = session.get(ClientSide, client_id)
    balance_now = today.balance if today else None
    snapshots = tuple(
        SendSnapshot(
            step_no=row.step_no,
            side=row.side_at_send,
            balance_band=_balance_band(row.balance_at_send),
            balance_since=_balance_since(row.balance_at_send, balance_now),
        )
        for row in rows
        if row.sent_at is not None
    )
    return SideChange(
        side_now=today.side if today else None,
        balance_band_now=_balance_band(balance_now),
        at_send=snapshots,
    )
