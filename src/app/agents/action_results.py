from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction

logger = structlog.get_logger(__name__)

SENT_STATUS = "sent"
DEPOSIT_TXN_TYPE = "purchase"
EDITED_OUTCOME = "edit_approve"
EVENT_FLAGS = {"open": "opened", "reply": "replied", "optout": "opted_out", "bounce": "bounced"}
BATCH_SIZE = 500
REMEASURED_COLUMNS = (
    "unit_fund_id",
    "opened",
    "replied",
    "opted_out",
    "bounced",
    "reviewer_changed",
    "deposited",
    "deposit_amount_kes",
    "measured_at",
)


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    client_id: int
    proposal_id: int
    insight_id: int | None
    sent_at: datetime


@dataclass(frozen=True)
class WindowOutcome:
    window_days: int
    measured: int


def measure_action_results(
    session: Session,
    *,
    now: datetime | None = None,
    windows: Sequence[int] | None = None,
    remeasure: bool = False,
) -> list[WindowOutcome]:
    settings = get_settings()
    measured_at = now or datetime.now(UTC)
    lengths = tuple(windows) if windows is not None else settings.action_result_windows
    loaded_at = _latest_active_load(session)
    measurable_until = None if loaded_at is None else min(measured_at, loaded_at)
    zone = ZoneInfo(settings.db_timezone)

    outcomes = [
        WindowOutcome(
            window_days=length,
            measured=_measure_window(
                session,
                length,
                measurable_until=measurable_until,
                measured_at=measured_at,
                remeasure=remeasure,
                zone=zone,
            ),
        )
        for length in lengths
    ]
    record_audit(
        session,
        entity_type="action_result",
        action="measure",
        detail={
            "windows": {str(outcome.window_days): outcome.measured for outcome in outcomes},
            "remeasure": remeasure,
            "data_loaded_at": None if loaded_at is None else loaded_at.isoformat(),
        },
    )
    session.commit()
    logger.info(
        "action_results.measured",
        windows={outcome.window_days: outcome.measured for outcome in outcomes},
        remeasure=remeasure,
        data_loaded_at=None if loaded_at is None else loaded_at.isoformat(),
    )
    return outcomes


def _latest_active_load(session: Session) -> datetime | None:
    return session.scalar(select(func.max(ActiveClientFund.updated_at)))


def _measure_window(
    session: Session,
    window_days: int,
    *,
    measurable_until: datetime | None,
    measured_at: datetime,
    remeasure: bool,
    zone: ZoneInfo,
) -> int:
    if measurable_until is None:
        return 0
    cutoff = measurable_until - timedelta(days=window_days)
    messages = _due_messages(session, window_days, cutoff, remeasure)
    saved = 0
    for batch in _batches(messages):
        rows = _result_rows(session, batch, window_days, measured_at, zone)
        _save(session, rows)
        session.commit()
        saved += len(rows)
    return saved


def _due_messages(
    session: Session, window_days: int, cutoff: datetime, remeasure: bool
) -> list[SentMessage]:
    stmt = (
        select(
            TouchLog.message_id,
            OutreachMessage.client_id,
            AgentProposal.proposal_id,
            AgentProposal.insight_id,
            TouchLog.sent_at,
        )
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .join(Campaign, Campaign.campaign_id == Enrollment.campaign_id)
        .join(AgentProposal, AgentProposal.campaign_id == Campaign.campaign_id)
        .join(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
        .where(
            TouchLog.delivery_status == SENT_STATUS,
            TouchLog.sent_at.is_not(None),
            TouchLog.sent_at <= cutoff,
            Campaign.is_test.is_(False),
        )
        .order_by(TouchLog.touch_id)
    )
    if not remeasure:
        stmt = stmt.where(
            TouchLog.message_id.not_in(
                select(ActionResult.message_id).where(ActionResult.window_days == window_days)
            )
        )
    return [SentMessage(*row) for row in session.execute(stmt).all()]


def _batches(messages: Sequence[SentMessage]) -> Iterator[Sequence[SentMessage]]:
    for start in range(0, len(messages), BATCH_SIZE):
        yield messages[start : start + BATCH_SIZE]


def _result_rows(
    session: Session,
    batch: Sequence[SentMessage],
    window_days: int,
    measured_at: datetime,
    zone: ZoneInfo,
) -> list[dict[str, Any]]:
    window = timedelta(days=window_days)
    client_ids = sorted({message.client_id for message in batch})
    funds = _included_funds(session, batch)
    balances = _balances(session, client_ids)
    edited = _edited_message_ids(session, [message.message_id for message in batch])
    events = _events_by_client(
        session,
        client_ids,
        earliest=min(message.sent_at for message in batch),
        latest=max(message.sent_at for message in batch) + window,
    )
    deposits = _deposits_by_client(
        session,
        client_ids,
        first_day=min(_local_day(message.sent_at, zone) for message in batch),
        last_day=max(_local_day(message.sent_at + window, zone) for message in batch),
    )

    rows = []
    for message in batch:
        fund_ids = funds.get((message.proposal_id, message.client_id))
        if not fund_ids:
            continue
        ends_at = message.sent_at + window
        total = _deposit_total(
            deposits.get(message.client_id, ()),
            fund_ids,
            first_day=_local_day(message.sent_at, zone),
            end_day=_local_day(ends_at, zone),
        )
        rows.append(
            {
                "proposal_id": message.proposal_id,
                "insight_id": message.insight_id,
                "client_id": message.client_id,
                "unit_fund_id": _primary_fund(message.client_id, fund_ids, balances),
                "message_id": message.message_id,
                "sent_at": message.sent_at,
                "window_days": window_days,
                **_event_flags(events.get(message.client_id, ()), message.sent_at, ends_at),
                "reviewer_changed": message.message_id in edited,
                "deposited": total > 0,
                "deposit_amount_kes": total,
                "measured_at": measured_at,
            }
        )
    return rows


def _local_day(moment: datetime, zone: ZoneInfo) -> date:
    return moment.astimezone(zone).date()


def _included_funds(
    session: Session, batch: Sequence[SentMessage]
) -> dict[tuple[int, int], list[int]]:
    rows = session.execute(
        select(
            AgentProposalClient.proposal_id,
            AgentProposalClient.client_id,
            AgentProposalClient.unit_fund_id,
        ).where(
            AgentProposalClient.proposal_id.in_(sorted({m.proposal_id for m in batch})),
            AgentProposalClient.client_id.in_(sorted({m.client_id for m in batch})),
            AgentProposalClient.included.is_(True),
        )
    ).all()
    funds: dict[tuple[int, int], list[int]] = defaultdict(list)
    for proposal_id, client_id, unit_fund_id in rows:
        funds[(proposal_id, client_id)].append(unit_fund_id)
    return funds


def _balances(session: Session, client_ids: Sequence[int]) -> dict[tuple[int, int], float]:
    rows = session.execute(
        select(
            ActiveClientFund.client_id, ActiveClientFund.unit_fund_id, ActiveClientFund.balance
        ).where(ActiveClientFund.client_id.in_(client_ids))
    ).all()
    return {(client_id, unit_fund_id): balance or 0.0 for client_id, unit_fund_id, balance in rows}


def _primary_fund(
    client_id: int, fund_ids: Iterable[int], balances: dict[tuple[int, int], float]
) -> int:
    return min(fund_ids, key=lambda fund_id: (-balances.get((client_id, fund_id), 0.0), fund_id))


def _edited_message_ids(session: Session, message_ids: Sequence[str]) -> set[str]:
    return set(
        session.scalars(
            select(ReviewAction.message_id).where(
                ReviewAction.message_id.in_(message_ids),
                ReviewAction.outcome == EDITED_OUTCOME,
            )
        )
    )


def _events_by_client(
    session: Session, client_ids: Sequence[int], *, earliest: datetime, latest: datetime
) -> dict[int, list[tuple[str, datetime]]]:
    rows = session.execute(
        select(ContactEvent.client_id, ContactEvent.type, ContactEvent.occurred_at).where(
            ContactEvent.client_id.in_(client_ids),
            ContactEvent.type.in_(list(EVENT_FLAGS)),
            ContactEvent.occurred_at >= earliest,
            ContactEvent.occurred_at < latest,
        )
    ).all()
    events: dict[int, list[tuple[str, datetime]]] = defaultdict(list)
    for client_id, kind, occurred_at in rows:
        events[client_id].append((kind, occurred_at))
    return events


def _event_flags(
    events: Iterable[tuple[str, datetime]], starts_at: datetime, ends_at: datetime
) -> dict[str, bool]:
    seen = {EVENT_FLAGS[kind] for kind, occurred_at in events if starts_at <= occurred_at < ends_at}
    return {flag: flag in seen for flag in EVENT_FLAGS.values()}


def _deposits_by_client(
    session: Session, client_ids: Sequence[int], *, first_day: date, last_day: date
) -> dict[int, list[tuple[int, date, float]]]:
    rows = session.execute(
        select(
            ActiveTransaction.client_id,
            ActiveTransaction.unit_fund_id,
            ActiveTransaction.txn_date,
            ActiveTransaction.amount,
        ).where(
            ActiveTransaction.client_id.in_(client_ids),
            ActiveTransaction.txn_type == DEPOSIT_TXN_TYPE,
            ActiveTransaction.txn_date >= first_day,
            ActiveTransaction.txn_date < last_day,
        )
    ).all()
    deposits: dict[int, list[tuple[int, date, float]]] = defaultdict(list)
    for client_id, unit_fund_id, txn_date, amount in rows:
        deposits[client_id].append((unit_fund_id, txn_date, amount))
    return deposits


def _deposit_total(
    deposits: Iterable[tuple[int, date, float]],
    fund_ids: Sequence[int],
    *,
    first_day: date,
    end_day: date,
) -> float:
    return sum(
        (
            amount
            for unit_fund_id, txn_date, amount in deposits
            if unit_fund_id in fund_ids and first_day <= txn_date < end_day
        ),
        0.0,
    )


def _save(session: Session, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    stmt = pg_insert(ActionResult).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["message_id", "window_days"],
        set_={column: getattr(stmt.excluded, column) for column in REMEASURED_COLUMNS},
    )
    session.execute(stmt)
