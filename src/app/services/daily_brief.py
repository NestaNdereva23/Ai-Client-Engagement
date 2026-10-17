from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select, true
from sqlalchemy.orm import Session

from app.agents.situations import FA_CALL_OVERDUE
from app.briefing.book import MonthEndPattern
from app.briefing.daily_brief import (
    BRIEF_GROUPS,
    CALENDAR_LABELS,
    NO_GROUP_RANK,
    STOPPED,
    WITHDRAWING,
    BriefClient,
    BriefGroup,
    CalendarItem,
    DailyBrief,
    Headline,
    below_line_headline,
    calendar_headline,
    client_reason,
    deposits_headline,
    half_balance_fact,
    large_withdrawals_headline,
    long_quiet_fact,
    overdue_calls_headline,
    warning_signs_fact,
)
from app.config import get_settings
from app.db.models.active_clients import (
    CHANNEL_BY_INTERACTION_TYPE,
    TOUCHPOINT_OPEN,
    ActiveClientFund,
    ActiveClientTouchpoint,
    ActiveTransaction,
)
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientSide, ClientTier
from app.db.models.signals import ClientSituationState
from app.risk.signals import SIGNAL_ORDER
from app.risk.store import load_active_config
from app.routing.sides import SIDE_ACTIVE, SIDE_INACTIVE
from app.services.book_briefing import gather_book_facts
from app.services.client_touchpoints import today_in_nairobi
from app.services.roster_ranking import SORT_BRIEF, build_roster_ranking

HEAVY_WINDOW_DAYS = 30
QUIET_DAYS = 180
EXAMPLES_PER_GROUP = 3
WHATSAPP_FOLLOW_UP = f"Follow up by {CHANNEL_BY_INTERACTION_TYPE['whatsapp_logged']}".lower()


def _run_day(computed_at: datetime | None, today: date) -> date:
    if computed_at is None:
        return today
    return computed_at.astimezone(ZoneInfo(get_settings().db_timezone)).date()


def _worklist_rows(session: Session, fa_id: str):
    ranking = build_roster_ranking(SORT_BRIEF)
    query = (
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            ActiveClientFund.client_code,
            ActiveClientFund.balance,
            ActiveClientFund.n_deposits,
            ActiveClientFund.last_deposit_date,
            ActiveClientFund.last_deposit_amount,
            ActiveClientFund.typical_gap_days,
            ActiveClientFund.largest_withdrawal,
            ActiveClientFund.last_withdrawal_date,
            ClientRiskFeatures.fund_at_risk,
            ClientRiskFeatures.sig_heavy_withdrawal,
            ClientRiskFeatures.sig_dormant,
            ClientRiskFeatures.sig_broken_pattern,
            ClientRiskFeatures.sig_shrinking,
            ClientRiskFeatures.sig_going_dormant,
            ClientRiskFeatures.sig_never_repeated,
        )
        .select_from(FaAssignment)
        .join(
            ActiveClientFund,
            and_(
                ActiveClientFund.client_id == FaAssignment.client_id,
                ActiveClientFund.unit_fund_id == FaAssignment.unit_fund_id,
            ),
        )
        .join(ClientTier, ClientTier.client_id == FaAssignment.client_id)
        .join(
            ClientRiskFeatures,
            and_(
                ClientRiskFeatures.client_id == ActiveClientFund.client_id,
                ClientRiskFeatures.unit_fund_id == ActiveClientFund.unit_fund_id,
            ),
            isouter=True,
        )
        .join(ranking.latest_handled_interaction, true(), isouter=True)
        .where(FaAssignment.fa_id == fa_id, ClientTier.segment.is_not(None))
        .add_columns(*ranking.select_columns())
        .order_by(*ranking.order_by_clauses())
    )
    return session.execute(query).all()


def _heavy_withdrawals(session: Session, fa_id: str, today: date):
    config = load_active_config(session, today)
    if config is None:
        return []
    fee_max = config.thresholds["SYSTEM_FEE_MAX"]
    heavy_share = config.thresholds["HEAVY_WITHDRAWAL_PCT"]
    return session.execute(
        select(ActiveTransaction.client_id, ActiveTransaction.amount, ActiveTransaction.txn_date)
        .join(
            ActiveClientFund,
            and_(
                ActiveClientFund.client_id == ActiveTransaction.client_id,
                ActiveClientFund.unit_fund_id == ActiveTransaction.unit_fund_id,
            ),
        )
        .join(
            FaAssignment,
            and_(
                FaAssignment.client_id == ActiveTransaction.client_id,
                FaAssignment.unit_fund_id == ActiveTransaction.unit_fund_id,
            ),
        )
        .where(
            FaAssignment.fa_id == fa_id,
            ActiveTransaction.txn_type == "sale",
            ActiveTransaction.amount > fee_max,
            ActiveTransaction.txn_date >= today - timedelta(days=HEAVY_WINDOW_DAYS),
            ActiveTransaction.amount
            >= heavy_share * (ActiveClientFund.balance + ActiveTransaction.amount),
        )
    ).all()


def _calendar_kind(kind: str, title: str) -> str:
    return "whatsapp" if title.lower().startswith(WHATSAPP_FOLLOW_UP) else kind


def _calendar(session: Session, fa_id: str, today: date) -> tuple[CalendarItem, ...]:
    rows = session.execute(
        select(
            ActiveClientTouchpoint.client_id,
            ActiveClientTouchpoint.kind,
            ActiveClientTouchpoint.title,
            ActiveClientTouchpoint.due_time,
        )
        .join(
            FaAssignment,
            and_(
                FaAssignment.client_id == ActiveClientTouchpoint.client_id,
                FaAssignment.unit_fund_id == ActiveClientTouchpoint.unit_fund_id,
            ),
        )
        .where(
            FaAssignment.fa_id == fa_id,
            ActiveClientTouchpoint.status == TOUCHPOINT_OPEN,
            ActiveClientTouchpoint.due_date == today,
        )
        .order_by(ActiveClientTouchpoint.due_time.nulls_last(), ActiveClientTouchpoint.id)
    ).all()
    items = []
    for row in rows:
        kind = _calendar_kind(row.kind, row.title)
        items.append(
            CalendarItem(
                client_id=row.client_id,
                kind=kind,
                label=CALENDAR_LABELS[kind],
                time=row.due_time.strftime("%H:%M") if row.due_time else None,
            )
        )
    return tuple(items)


def _fell_below_line(session: Session, fa_id: str, since: date) -> int:
    return session.scalar(
        select(func.count(func.distinct(ClientSide.client_id)))
        .join(FaAssignment, FaAssignment.client_id == ClientSide.client_id)
        .where(
            FaAssignment.fa_id == fa_id,
            ClientSide.side == SIDE_INACTIVE,
            ClientSide.previous_side == SIDE_ACTIVE,
            func.date(ClientSide.side_changed_at) >= since,
        )
    )


def _overdue_calls(session: Session, fa_id: str) -> int:
    return session.scalar(
        select(func.count(func.distinct(ClientSituationState.client_id)))
        .join(
            FaAssignment,
            and_(
                FaAssignment.client_id == ClientSituationState.client_id,
                FaAssignment.unit_fund_id == ClientSituationState.unit_fund_id,
            ),
        )
        .where(
            FaAssignment.fa_id == fa_id,
            ClientSituationState.situation_code == FA_CALL_OVERDUE,
            ClientSituationState.is_active.is_(True),
        )
    )


def _deposits(session: Session, fa_id: str, since: date, until: date) -> tuple[int, float]:
    clients, amount = session.execute(
        select(
            func.count(func.distinct(ActiveTransaction.client_id)),
            func.coalesce(func.sum(ActiveTransaction.amount), 0.0),
        )
        .join(
            FaAssignment,
            and_(
                FaAssignment.client_id == ActiveTransaction.client_id,
                FaAssignment.unit_fund_id == ActiveTransaction.unit_fund_id,
            ),
        )
        .where(
            FaAssignment.fa_id == fa_id,
            ActiveTransaction.txn_type == "purchase",
            ActiveTransaction.txn_date >= since,
            ActiveTransaction.txn_date < until,
        )
    ).one()
    return clients, amount


def _headlines(
    session: Session,
    fa_id: str,
    withdrawals: Sequence,
    calendar: Sequence[CalendarItem],
    since: date,
    until: date,
    when: str,
) -> tuple[Headline, ...]:
    headlines: list[Headline] = []
    recent = [withdrawal for withdrawal in withdrawals if since <= withdrawal.txn_date < until]
    if recent:
        headlines.append(
            large_withdrawals_headline(
                len({withdrawal.client_id for withdrawal in recent}),
                sum(withdrawal.amount for withdrawal in recent),
                when,
            )
        )
    if calendar:
        headlines.append(calendar_headline(calendar))
    below_line = _fell_below_line(session, fa_id, since)
    if below_line:
        headlines.append(below_line_headline(below_line))
    overdue = _overdue_calls(session, fa_id)
    if overdue:
        headlines.append(overdue_calls_headline(overdue))
    deposit_clients, deposit_amount = _deposits(session, fa_id, since, until)
    if deposit_clients:
        headlines.append(deposits_headline(deposit_clients, deposit_amount, when))
    return tuple(headlines)


def _examples(code: str, members: Sequence, today: date) -> tuple[BriefClient, ...]:
    examples: list[BriefClient] = []
    seen: set[int] = set()
    for row in members:
        if row.client_id in seen:
            continue
        seen.add(row.client_id)
        examples.append(
            BriefClient(
                client_id=row.client_id,
                unit_fund_id=row.unit_fund_id,
                client_code=row.client_code,
                balance_kes=row.balance,
                fund_at_risk_kes=row.fund_at_risk,
                reason=client_reason(code, row, today),
            )
        )
        if len(examples) == EXAMPLES_PER_GROUP:
            break
    return tuple(examples)


def _sharp_fact(
    code: str,
    members: Sequence,
    client_ids: Sequence[int],
    withdrawals: Sequence,
    today: date,
    since: date,
    until: date,
    when: str,
) -> str | None:
    if code == WITHDRAWING:
        wanted = set(client_ids)
        hits = [withdrawal for withdrawal in withdrawals if withdrawal.client_id in wanted]
        recent = {
            withdrawal.client_id for withdrawal in hits if since <= withdrawal.txn_date < until
        }
        return half_balance_fact(
            len({withdrawal.client_id for withdrawal in hits}),
            sum(withdrawal.amount for withdrawal in hits),
            len(recent),
            when,
        )
    if code == STOPPED:
        latest: dict[int, date] = {}
        for row in members:
            if row.last_deposit_date is not None:
                latest[row.client_id] = max(
                    latest.get(row.client_id, row.last_deposit_date), row.last_deposit_date
                )
        cutoff = today - timedelta(days=QUIET_DAYS)
        return long_quiet_fact(sum(1 for last in latest.values() if last < cutoff))
    return None


def _signal_counts(members: Sequence) -> dict[str, int]:
    clients_by_signal: dict[str, set[int]] = {name: set() for name in SIGNAL_ORDER}
    for row in members:
        for name in SIGNAL_ORDER:
            if getattr(row, name):
                clients_by_signal[name].add(row.client_id)
    return {name: len(ids) for name, ids in clients_by_signal.items() if ids}


def _groups(
    rows: Sequence,
    withdrawals: Sequence,
    total_at_risk: float,
    today: date,
    since: date,
    until: date,
    when: str,
) -> tuple[tuple[BriefGroup, ...], int, float]:
    by_rank: dict[int, list] = defaultdict(list)
    for row in rows:
        by_rank[row.sort_group_rank].append(row)
    groups = []
    for rank, spec in enumerate(BRIEF_GROUPS):
        members = by_rank.get(rank)
        if not members:
            continue
        client_ids = list(dict.fromkeys(row.client_id for row in members))
        at_risk = sum(row.fund_at_risk or 0.0 for row in members)
        groups.append(
            BriefGroup(
                code=spec.code,
                title=spec.title,
                clients=len(client_ids),
                could_leave_kes=at_risk,
                share=at_risk / total_at_risk if total_at_risk else 0.0,
                what_is_happening=spec.happening,
                what_to_do=spec.todo,
                worth_knowing=spec.unknown,
                sharp_fact=_sharp_fact(
                    spec.code, members, client_ids, withdrawals, today, since, until, when
                ),
                examples=_examples(spec.code, members, today),
                warning_signs=warning_signs_fact(_signal_counts(members), len(client_ids)),
            )
        )
    rest = by_rank.get(NO_GROUP_RANK, [])
    return (
        tuple(groups),
        len({row.client_id for row in rest}),
        sum(row.fund_at_risk or 0.0 for row in rest),
    )


def build_daily_brief(
    session: Session,
    fa_id: str,
    *,
    today: date | None = None,
    advisor_name: str | None = None,
    month_end: MonthEndPattern | None = None,
) -> DailyBrief:
    today = today or today_in_nairobi()
    facts = gather_book_facts(session, fa_id, advisor_name=advisor_name, month_end=month_end)
    run_day = _run_day(facts.computed_at, today)
    fresh = run_day >= today - timedelta(days=1)
    since = (today if fresh else run_day) - timedelta(days=1)
    when = "yesterday" if fresh else f"since {since.day} {since:%B}"
    withdrawals = _heavy_withdrawals(session, fa_id, today)
    calendar = _calendar(session, fa_id, today)
    groups, rest_clients, rest_at_risk = _groups(
        _worklist_rows(session, fa_id),
        withdrawals,
        facts.worklist_at_risk_kes,
        today,
        since,
        today,
        when,
    )
    return DailyBrief(
        fa_id=fa_id,
        advisor_name=facts.advisor_name,
        as_of=run_day,
        assigned_clients=facts.assigned_clients,
        worklist_clients=facts.worklist_clients,
        at_risk_kes=facts.worklist_at_risk_kes,
        headlines=_headlines(session, fa_id, withdrawals, calendar, since, today, when),
        calendar=calendar,
        groups=groups,
        rest_clients=rest_clients,
        rest_could_leave_kes=rest_at_risk,
        month_end=month_end,
    )
