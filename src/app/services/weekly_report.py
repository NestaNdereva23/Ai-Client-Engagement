from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import groupby
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import Date, and_, cast, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.advisors import FaRecord
from app.briefing.book import compact_money
from app.config import get_settings
from app.db.models.active_clients import (
    CHANNEL_BY_INTERACTION_TYPE,
    CONTACT_INTERACTION_TYPES,
    FLAGGED_FOR_ACCOUNT_MANAGER,
    TOUCHPOINT_OPEN,
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveClientTouchpoint,
    ActiveTransaction,
)
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import RiskSnapshot
from app.risk.store import load_active_config
from app.services.clients import get_client_names
from app.services.fund_names import fund_short_names

LOOKBACK_DAYS = 28
# A withdrawal lands in the feed before the run that flags the client for it.
WITHDRAWAL_LEAD_DAYS = 3
FOLLOW_UP_HORIZON_DAYS = 7
SMALL_BALANCE_ROUTE = "small_balance_review"
NO_ANSWER = "no_answer"
REACHED = "reached"
TRIED = "tried"
NOT_REACHED = "not_reached"
NAMES_ACTOR = "weekly_report"

Key = tuple[int, int]
NameLookup = Callable[..., dict[int, str | None]]


def report_week(today: date) -> tuple[date, date]:
    return today - timedelta(days=7), today - timedelta(days=1)


def day_label(day: date) -> str:
    return f"{day.day} {day:%b}"


@dataclass(frozen=True)
class FlaggedClient:
    client_id: int
    unit_fund_id: int
    name: str
    detail: str
    reason: str
    risk_score: int
    risk_band: str
    at_risk_kes: float
    balance: float
    flagged_on: date
    days_open: int
    status: str
    attempts: int
    on_record: str
    withdrawn_kes: float
    days_to_contact: float | None


@dataclass
class MoneyLine:
    clients: int = 0
    deposits: int = 0
    money_in: float = 0.0
    withdrawals: int = 0
    money_out: float = 0.0

    @property
    def net(self) -> float:
        return self.money_in - self.money_out

    def add(self, other: MoneyLine) -> None:
        self.clients += other.clients
        self.deposits += other.deposits
        self.money_in += other.money_in
        self.withdrawals += other.withdrawals
        self.money_out += other.money_out


@dataclass
class LoggedLine:
    channel: str
    total: int = 0
    connected: int = 0
    follow_up: int = 0
    no_answer: int = 0


@dataclass(frozen=True)
class FollowUp:
    name: str
    title: str
    due_date: date
    overdue_days: int


@dataclass
class AfterContact:
    clients: int = 0
    deposited: int = 0
    deposited_kes: float = 0.0
    withdrew: int = 0
    withdrew_kes: float = 0.0

    @property
    def no_change(self) -> int:
        return self.clients - self.deposited - self.withdrew


@dataclass(frozen=True)
class WeekTotals:
    flagged: int
    reached: int
    median_days: float | None
    money: MoneyLine

    @property
    def reached_pct(self) -> int | None:
        return round(100 * self.reached / self.flagged) if self.flagged else None


@dataclass
class AdvisorReport:
    advisor: FaRecord
    week_start: date
    week_end: date
    book_clients: int = 0
    flagged: list[FlaggedClient] = field(default_factory=list)
    logged: list[LoggedLine] = field(default_factory=list)
    snoozed: int = 0
    dismissed: int = 0
    passed_on: int = 0
    contacts_with_note: int = 0
    follow_ups: list[FollowUp] = field(default_factory=list)
    money_reached: MoneyLine = field(default_factory=MoneyLine)
    money_not_reached: MoneyLine = field(default_factory=MoneyLine)
    money_rest: MoneyLine = field(default_factory=MoneyLine)
    after_contact: AfterContact = field(default_factory=AfterContact)
    previous: WeekTotals | None = None

    def _with_status(self, status: str) -> list[FlaggedClient]:
        return [client for client in self.flagged if client.status == status]

    @property
    def reached(self) -> list[FlaggedClient]:
        return self._with_status(REACHED)

    @property
    def tried(self) -> list[FlaggedClient]:
        return self._with_status(TRIED)

    @property
    def not_reached(self) -> list[FlaggedClient]:
        return self._with_status(NOT_REACHED)

    @property
    def new_flags(self) -> int:
        return sum(1 for client in self.flagged if client.flagged_on >= self.week_start)

    @property
    def median_days(self) -> float | None:
        days = [c.days_to_contact for c in self.flagged if c.days_to_contact is not None]
        return median(days) if days else None

    @property
    def contacts(self) -> int:
        return sum(line.total for line in self.logged)

    @property
    def overdue_follow_ups(self) -> int:
        return sum(1 for follow_up in self.follow_ups if follow_up.overdue_days > 0)

    @property
    def money_total(self) -> MoneyLine:
        total = MoneyLine()
        for line in (self.money_reached, self.money_not_reached, self.money_rest):
            total.add(line)
        return total

    @property
    def totals(self) -> WeekTotals:
        return WeekTotals(
            flagged=len(self.flagged),
            reached=len(self.reached),
            median_days=self.median_days,
            money=self.money_total,
        )


def _at(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(get_settings().db_timezone))


def _same_fund(left, right):
    return and_(left.client_id == right.client_id, left.unit_fund_id == right.unit_fund_id)


def _in_keys(model, keys: Sequence[Key]):
    return tuple_(model.client_id, model.unit_fund_id).in_(list(keys))


def _book(session: Session, fa_ids: Sequence[str]) -> dict[Key, tuple[str, str | None, float]]:
    rows = session.execute(
        select(
            FaAssignment.fa_id,
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            ActiveClientFund.client_code,
            ActiveClientFund.balance,
        )
        .join(ActiveClientFund, _same_fund(ActiveClientFund, FaAssignment))
        .where(FaAssignment.fa_id.in_(fa_ids))
    )
    return {(r.client_id, r.unit_fund_id): (r.fa_id, r.client_code, r.balance or 0.0) for r in rows}


def _flags(session: Session, fa_ids: Sequence[str], min_score: int, lookback, start, end):
    is_flagged = and_(
        RiskSnapshot.risk_score >= min_score,
        func.coalesce(RiskSnapshot.route, "") != SMALL_BALANCE_ROUTE,
    )
    keys = [
        (row.client_id, row.unit_fund_id)
        for row in session.execute(
            select(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id)
            .join(FaAssignment, _same_fund(FaAssignment, RiskSnapshot))
            .where(
                FaAssignment.fa_id.in_(fa_ids),
                RiskSnapshot.created_at >= start,
                RiskSnapshot.created_at < end,
                is_flagged,
            )
            .distinct()
        )
    ]
    if not keys:
        return {}
    rows = session.execute(
        select(
            RiskSnapshot.client_id,
            RiskSnapshot.unit_fund_id,
            RiskSnapshot.created_at,
            RiskSnapshot.risk_score,
            RiskSnapshot.risk_band,
            RiskSnapshot.risk_reasons,
            RiskSnapshot.fund_at_risk,
            is_flagged.label("flagged"),
        )
        .where(
            _in_keys(RiskSnapshot, keys),
            RiskSnapshot.created_at >= lookback,
            RiskSnapshot.created_at < end,
        )
        .order_by(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id, RiskSnapshot.created_at.desc())
    ).all()
    flags = {}
    for key, snapshots in groupby(rows, key=lambda r: (r.client_id, r.unit_fund_id)):
        latest = since = None
        for snapshot in snapshots:
            if latest is None:
                if snapshot.flagged:
                    latest, since = snapshot, snapshot.created_at
                continue
            if not snapshot.flagged:
                break
            since = snapshot.created_at
        if latest is not None:
            flags[key] = (latest, since)
    return flags


def _interactions(session: Session, keys: Sequence[Key], lookback, end):
    found = defaultdict(list)
    if not keys:
        return found
    rows = session.execute(
        select(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            ActiveClientInteraction.type,
            ActiveClientInteraction.outcome,
            ActiveClientInteraction.note,
            ActiveClientInteraction.created_at,
        )
        .where(
            _in_keys(ActiveClientInteraction, keys),
            ActiveClientInteraction.created_at >= lookback,
            ActiveClientInteraction.created_at < end,
        )
        .order_by(ActiveClientInteraction.created_at)
    )
    for row in rows:
        found[(row.client_id, row.unit_fund_id)].append(row)
    return found


def _real_withdrawal(fee_max: float):
    return and_(ActiveTransaction.txn_type == "sale", ActiveTransaction.amount > fee_max)


def _withdrawals(session: Session, keys: Sequence[Key], fee_max: float, since: date):
    found = defaultdict(list)
    if not keys:
        return found
    rows = session.execute(
        select(
            ActiveTransaction.client_id,
            ActiveTransaction.unit_fund_id,
            ActiveTransaction.amount,
            ActiveTransaction.txn_date,
        ).where(
            _in_keys(ActiveTransaction, keys),
            _real_withdrawal(fee_max),
            ActiveTransaction.txn_date >= since,
        )
    )
    for row in rows:
        found[(row.client_id, row.unit_fund_id)].append(row)
    return found


def _on_record(status: str, contacts: list, others: list) -> str:
    if status == REACHED:
        return CHANNEL_BY_INTERACTION_TYPE[contacts[-1].type].capitalize()
    if status == TRIED:
        note = contacts[-1].note
        tries = f"{len(contacts)} attempt{'' if len(contacts) == 1 else 's'}, no answer"
        return f"{tries}. {note.strip()}" if note and note.strip() else tries
    if not others:
        return "Nothing logged"
    last = others[-1]
    label = last.type.replace("_", " ").capitalize()
    note = "" if last.note and last.note.strip() else ", no note"
    return f"{label} on {day_label(last.created_at.date())}{note}"


def _flagged_client(key, book_row, flag, items, sales, name, fund, week_end) -> FlaggedClient:
    snapshot, since = flag
    flagged_on = since.date()
    items = [item for item in items if item.created_at >= since]
    contacts = [item for item in items if item.type in CONTACT_INTERACTION_TYPES]
    others = [item for item in items if item.type not in CONTACT_INTERACTION_TYPES]
    reaching = [item for item in contacts if item.outcome != NO_ANSWER]
    status = REACHED if reaching else TRIED if contacts else NOT_REACHED
    sales = [s for s in sales if s.txn_date >= flagged_on - timedelta(days=WITHDRAWAL_LEAD_DAYS)]
    reason = snapshot.risk_reasons
    if sales:
        largest = max(sales, key=lambda sale: sale.amount)
        reason = f"Withdrew {compact_money(largest.amount)} on {day_label(largest.txn_date)}"
    _fa_id, client_code, balance = book_row
    return FlaggedClient(
        client_id=key[0],
        unit_fund_id=key[1],
        name=name or client_code or str(key[0]),
        detail=" - ".join(part for part in (client_code, fund) if part),
        reason=reason,
        risk_score=snapshot.risk_score,
        risk_band=snapshot.risk_band,
        at_risk_kes=snapshot.fund_at_risk,
        balance=balance,
        flagged_on=flagged_on,
        days_open=(week_end - flagged_on).days + 1,
        status=status,
        attempts=len(contacts),
        on_record=_on_record(status, reaching or contacts, others),
        withdrawn_kes=sum(sale.amount for sale in sales),
        days_to_contact=(
            (reaching[0].created_at - since).total_seconds() / 86400 if reaching else None
        ),
    )


def _add_logged(session: Session, reports: dict[str, AdvisorReport], start, end) -> None:
    has_note = func.btrim(func.coalesce(ActiveClientInteraction.note, "")) != ""
    rows = session.execute(
        select(
            FaAssignment.fa_id,
            ActiveClientInteraction.type,
            ActiveClientInteraction.outcome,
            func.count(),
            func.count().filter(has_note),
        )
        .join(FaAssignment, _same_fund(FaAssignment, ActiveClientInteraction))
        .where(
            FaAssignment.fa_id.in_(list(reports)),
            ActiveClientInteraction.created_at >= start,
            ActiveClientInteraction.created_at < end,
        )
        .group_by(FaAssignment.fa_id, ActiveClientInteraction.type, ActiveClientInteraction.outcome)
    )
    lines: dict[tuple[str, str], LoggedLine] = {}
    for fa_id, kind, outcome, count, noted in rows:
        report = reports[fa_id]
        if kind == "snoozed":
            report.snoozed += count
        elif kind == "dismissed":
            report.dismissed += count
        elif kind == FLAGGED_FOR_ACCOUNT_MANAGER:
            report.passed_on += count
        elif kind in CHANNEL_BY_INTERACTION_TYPE:
            channel = CHANNEL_BY_INTERACTION_TYPE[kind]
            line = lines.setdefault((fa_id, channel), LoggedLine(channel=channel))
            line.total += count
            report.contacts_with_note += noted
            if outcome == NO_ANSWER:
                line.no_answer += count
            elif outcome == "follow_up":
                line.follow_up += count
            else:
                line.connected += count
    for (fa_id, _channel), line in sorted(lines.items(), key=lambda item: -item[1].total):
        reports[fa_id].logged.append(line)


def _follow_up_rows(session: Session, fa_ids: Sequence[str], week_end: date):
    return session.execute(
        select(
            FaAssignment.fa_id,
            ActiveClientTouchpoint.client_id,
            ActiveClientTouchpoint.title,
            ActiveClientTouchpoint.due_date,
        )
        .join(FaAssignment, _same_fund(FaAssignment, ActiveClientTouchpoint))
        .where(
            FaAssignment.fa_id.in_(fa_ids),
            ActiveClientTouchpoint.status == TOUCHPOINT_OPEN,
            ActiveClientTouchpoint.due_date <= week_end + timedelta(days=FOLLOW_UP_HORIZON_DAYS),
        )
        .order_by(ActiveClientTouchpoint.due_date)
    ).all()


def _add_money(
    session: Session,
    reports: dict[str, AdvisorReport],
    status_by_key: dict[Key, str],
    fee_max: float,
    week_start: date,
    week_end: date,
) -> dict[str, MoneyLine]:
    this_week = (ActiveTransaction.txn_date >= week_start).label("this_week")
    rows = session.execute(
        select(
            FaAssignment.fa_id,
            ActiveTransaction.client_id,
            ActiveTransaction.unit_fund_id,
            this_week,
            ActiveTransaction.txn_type,
            func.sum(ActiveTransaction.amount),
            func.count(),
        )
        .join(FaAssignment, _same_fund(FaAssignment, ActiveTransaction))
        .where(
            FaAssignment.fa_id.in_(list(reports)),
            ActiveTransaction.txn_date >= week_start - timedelta(days=7),
            ActiveTransaction.txn_date <= week_end,
            or_(ActiveTransaction.txn_type == "purchase", _real_withdrawal(fee_max)),
        )
        .group_by(
            FaAssignment.fa_id,
            ActiveTransaction.client_id,
            ActiveTransaction.unit_fund_id,
            this_week,
            ActiveTransaction.txn_type,
        )
    )
    last_week: dict[str, MoneyLine] = defaultdict(MoneyLine)
    for fa_id, client_id, unit_fund_id, in_week, txn_type, amount, count in rows:
        report = reports[fa_id]
        if not in_week:
            line = last_week[fa_id]
        else:
            status = status_by_key.get((client_id, unit_fund_id))
            line = (
                report.money_rest
                if status is None
                else report.money_reached
                if status == REACHED
                else report.money_not_reached
            )
        if txn_type == "purchase":
            line.deposits += count
            line.money_in += amount
        else:
            line.withdrawals += count
            line.money_out += amount
    return last_week


def _add_after_contact(
    session: Session, reports: dict[str, AdvisorReport], fee_max: float, lookback, end
) -> None:
    reach = (
        select(
            ActiveClientInteraction.client_id,
            ActiveClientInteraction.unit_fund_id,
            func.min(ActiveClientInteraction.created_at).label("first_at"),
        )
        .where(
            ActiveClientInteraction.type.in_(CONTACT_INTERACTION_TYPES),
            func.coalesce(ActiveClientInteraction.outcome, "") != NO_ANSWER,
            ActiveClientInteraction.created_at >= lookback,
            ActiveClientInteraction.created_at < end,
        )
        .group_by(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id)
        .subquery()
    )
    after = ActiveTransaction.txn_date >= cast(reach.c.first_at, Date)
    rows = session.execute(
        select(
            FaAssignment.fa_id,
            func.coalesce(
                func.sum(ActiveTransaction.amount).filter(
                    ActiveTransaction.txn_type == "purchase", after
                ),
                0.0,
            ),
            func.coalesce(
                func.sum(ActiveTransaction.amount).filter(_real_withdrawal(fee_max), after), 0.0
            ),
        )
        .select_from(reach)
        .join(FaAssignment, _same_fund(FaAssignment, reach.c))
        .outerjoin(ActiveTransaction, _same_fund(ActiveTransaction, reach.c))
        .where(FaAssignment.fa_id.in_(list(reports)))
        .group_by(FaAssignment.fa_id, reach.c.client_id, reach.c.unit_fund_id)
    )
    for fa_id, deposited, withdrew in rows:
        result = reports[fa_id].after_contact
        result.clients += 1
        if deposited > 0:
            result.deposited += 1
            result.deposited_kes += deposited
        elif withdrew > 0:
            result.withdrew += 1
            result.withdrew_kes += withdrew


def build_weekly_reports(
    session: Session,
    advisors: Sequence[FaRecord],
    week_start: date,
    week_end: date,
    *,
    client_names: NameLookup = get_client_names,
    compare: bool = True,
) -> list[AdvisorReport]:
    reports = {
        advisor.fa_id: AdvisorReport(advisor=advisor, week_start=week_start, week_end=week_end)
        for advisor in advisors
    }
    if not reports:
        return []
    fa_ids = list(reports)
    config = load_active_config(session, week_end)
    fee_max = float(config.thresholds["SYSTEM_FEE_MAX"]) if config else 0.0
    start, end = _at(week_start), _at(week_end + timedelta(days=1))
    lookback = start - timedelta(days=LOOKBACK_DAYS)

    book = _book(session, fa_ids)
    for fa_id, _code, _balance in book.values():
        reports[fa_id].book_clients += 1

    flags = _flags(session, fa_ids, config.at_risk_min, lookback, start, end) if config else {}
    flags = {key: flag for key, flag in flags.items() if key in book}
    keys = list(flags)
    interactions = _interactions(session, keys, lookback, end)
    withdrawals = _withdrawals(
        session, keys, fee_max, lookback.date() - timedelta(days=WITHDRAWAL_LEAD_DAYS)
    )
    follow_up_rows = _follow_up_rows(session, fa_ids, week_end)
    names = client_names(
        sorted({key[0] for key in keys} | {row.client_id for row in follow_up_rows}),
        reviewer_id=NAMES_ACTOR,
    )
    funds = fund_short_names(session, {key[1] for key in keys})

    status_by_key: dict[Key, str] = {}
    for key, flag in flags.items():
        client = _flagged_client(
            key,
            book[key],
            flag,
            interactions[key],
            withdrawals[key],
            names.get(key[0]),
            funds.get(key[1]),
            week_end,
        )
        status_by_key[key] = client.status
        report = reports[book[key][0]]
        report.flagged.append(client)
        line = report.money_reached if client.status == REACHED else report.money_not_reached
        line.clients += 1
    for report in reports.values():
        report.flagged.sort(key=lambda c: (-c.withdrawn_kes, -c.at_risk_kes))
        report.money_rest.clients = report.book_clients - len(report.flagged)

    report_day = week_end + timedelta(days=1)
    for row in follow_up_rows:
        reports[row.fa_id].follow_ups.append(
            FollowUp(
                name=names.get(row.client_id) or str(row.client_id),
                title=row.title,
                due_date=row.due_date,
                overdue_days=max(0, (report_day - row.due_date).days),
            )
        )

    _add_logged(session, reports, start, end)
    last_week_money = _add_money(session, reports, status_by_key, fee_max, week_start, week_end)
    _add_after_contact(session, reports, fee_max, lookback, end)

    if compare:
        earlier = build_weekly_reports(
            session,
            advisors,
            week_start - timedelta(days=7),
            week_end - timedelta(days=7),
            client_names=lambda ids, **_: {},
            compare=False,
        )
        for before in earlier:
            totals = before.totals
            reports[before.advisor.fa_id].previous = WeekTotals(
                flagged=totals.flagged,
                reached=totals.reached,
                median_days=totals.median_days,
                money=last_week_money[before.advisor.fa_id],
            )
    return list(reports.values())
