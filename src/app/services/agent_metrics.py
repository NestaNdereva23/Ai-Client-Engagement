from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import ceil, floor
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal
from app.db.models.agent_run import ACTION_AGENT, BOOK_WIDE_AGENTS, AgentRun
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.complaints import ClientComplaint
from app.db.models.llmops import LLMResponse
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.signals import ClientSignalState
from app.schemas.agent_metrics import (
    BookSize,
    CostOut,
    CoverageOut,
    DashboardOut,
    DismissReason,
    FreedomOut,
    FreedomWeek,
    GrowthFoundOut,
    LatencyStat,
    LossesPreventedOut,
    MetricWindow,
    MoneyAtRiskOut,
    QualityOut,
    QualityWeek,
    RunDurationStat,
    SpeedBuckets,
    SpeedOut,
    SystemSpeedOut,
    UsefulnessOut,
)

RISK_KIND = "risk"
OPPORTUNITY_KIND = "opportunity"
SENT_STATUS = "sent"
OPTOUT_EVENT = "optout"
EDIT_OUTCOME = "edit_approve"
REJECT_OUTCOME = "reject"
WEEK = timedelta(days=7)
PERMISSION_LEVELS = ("suggest_only", "approve_each", "approve_sample", "act_alone")


def resolve_window(since: date | None, until: date | None, window_days: int | None) -> MetricWindow:
    settings = get_settings()
    end = until or date.today()
    start = since or (end - timedelta(days=settings.metrics_lookback_days))
    if start > end:
        start = end
    days = window_days or settings.metrics_window_days
    return MetricWindow(since=start, until=end, window_days=days)


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().db_timezone)


def _bounds(window: MetricWindow) -> tuple[datetime, datetime]:
    zone = _zone()
    start = datetime.combine(window.since, time.min, zone)
    end = datetime.combine(window.until + timedelta(days=1), time.min, zone)
    return start, end


def _local_day(moment: datetime) -> date:
    return moment.astimezone(_zone()).date()


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_range(window: MetricWindow) -> list[date]:
    weeks = []
    cursor = _week_start(window.since)
    last = window.until
    while cursor <= last:
        weeks.append(cursor)
        cursor = cursor + WEEK
    return weeks


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    low = floor(position)
    high = ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def _ratio(part: int, whole: int) -> float | None:
    if whole == 0:
        return None
    return part / whole


def speed_metric(session: Session, window: MetricWindow) -> SpeedOut:
    start, end = _bounds(window)
    rows = session.execute(
        select(ActionResult.message_id, AgentInsight.created_at, ActionResult.sent_at)
        .join(AgentInsight, AgentInsight.insight_id == ActionResult.insight_id)
        .where(ActionResult.sent_at >= start, ActionResult.sent_at < end)
        .distinct()
    ).all()

    days = []
    for _message_id, found_at, sent_at in rows:
        gap = (sent_at - found_at).total_seconds() / 86400.0
        days.append(gap if gap > 0 else 0.0)

    buckets = SpeedBuckets(
        same_day=sum(1 for value in days if value < 1),
        within_three_days=sum(1 for value in days if 1 <= value < 3),
        within_a_week=sum(1 for value in days if 3 <= value <= 7),
        over_a_week=sum(1 for value in days if value > 7),
    )
    average = sum(days) / len(days) if days else None
    return SpeedOut(
        window=window,
        sample=len(days),
        average_days=average,
        median_days=_percentile(days, 0.5),
        p90_days=_percentile(days, 0.9),
        buckets=buckets,
    )


def coverage_metric(session: Session, window: MetricWindow) -> CoverageOut:
    found = session.execute(
        select(ClientSignalState.client_id, func.min(ClientSignalState.since))
        .where(
            ClientSignalState.is_active.is_(True),
            ClientSignalState.since >= window.since,
            ClientSignalState.since <= window.until,
        )
        .group_by(ClientSignalState.client_id)
    ).all()
    found_by_client = {client_id: since for client_id, since in found}

    if not found_by_client:
        return CoverageOut(
            window=window, clients_found=0, contacted_within_a_week=0, coverage_rate=None
        )

    sends = session.execute(
        select(OutreachMessage.client_id, func.min(TouchLog.sent_at))
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .join(Campaign, Campaign.campaign_id == Enrollment.campaign_id)
        .join(OutreachMessage, OutreachMessage.message_id == TouchLog.message_id)
        .where(
            TouchLog.delivery_status == SENT_STATUS,
            TouchLog.sent_at.is_not(None),
            Campaign.is_test.is_(False),
            OutreachMessage.client_id.in_(list(found_by_client)),
        )
        .group_by(OutreachMessage.client_id)
    ).all()
    first_send = {client_id: _local_day(sent_at) for client_id, sent_at in sends}

    contacted = 0
    for client_id, found_day in found_by_client.items():
        sent_day = first_send.get(client_id)
        if sent_day is not None and found_day <= sent_day <= found_day + WEEK:
            contacted += 1

    return CoverageOut(
        window=window,
        clients_found=len(found_by_client),
        contacted_within_a_week=contacted,
        coverage_rate=_ratio(contacted, len(found_by_client)),
    )


def _risk_rows(session: Session, window: MetricWindow, kind: str):
    start, end = _bounds(window)
    return session.execute(
        select(
            ActionResult.client_id,
            ActionResult.unit_fund_id,
            ActionResult.deposited,
            ActionResult.deposit_amount_kes,
            ActiveClientFund.balance,
        )
        .join(AgentInsight, AgentInsight.insight_id == ActionResult.insight_id)
        .outerjoin(
            ActiveClientFund,
            and_(
                ActiveClientFund.client_id == ActionResult.client_id,
                ActiveClientFund.unit_fund_id == ActionResult.unit_fund_id,
            ),
        )
        .where(
            ActionResult.window_days == window.window_days,
            ActionResult.sent_at >= start,
            ActionResult.sent_at < end,
            AgentInsight.kind == kind,
        )
    ).all()


def _client_totals(session: Session, client_ids: list[int]) -> dict[int, tuple[float, int]]:
    if not client_ids:
        return {}
    rows = session.execute(
        select(
            ActiveClientFund.client_id,
            func.coalesce(func.sum(ActiveClientFund.balance), 0.0),
            func.count(),
        )
        .where(ActiveClientFund.client_id.in_(client_ids))
        .group_by(ActiveClientFund.client_id)
    ).all()
    return {client_id: (float(total), int(count)) for client_id, total, count in rows}


def money_at_risk_metric(session: Session, window: MetricWindow) -> MoneyAtRiskOut:
    rows = _risk_rows(session, window, RISK_KIND)
    by_fund: dict[tuple[int, int], tuple[bool, float, float]] = {}
    for client_id, fund_id, deposited, amount, balance in rows:
        by_fund[(client_id, fund_id)] = (
            bool(deposited),
            float(amount or 0.0),
            float(balance or 0.0),
        )

    clients = {client_id for client_id, _fund_id in by_fund}
    money_reached = sum(balance for _deposited, _amount, balance in by_fund.values())
    deposited_clients = {
        client_id for (client_id, _fund_id), (deposited, _a, _b) in by_fund.items() if deposited
    }
    deposited_money = sum(amount for (deposited, amount, _balance) in by_fund.values() if deposited)
    return MoneyAtRiskOut(
        window=window,
        clients_reached=len(clients),
        money_reached_kes=money_reached,
        deposited_clients=len(deposited_clients),
        deposited_money_kes=deposited_money,
    )


def losses_prevented_metric(session: Session, window: MetricWindow) -> LossesPreventedOut:
    rows = _risk_rows(session, window, RISK_KIND)
    deposit_by_client: dict[int, float] = defaultdict(float)
    deposited_clients: set[int] = set()
    contacted: set[int] = set()
    for client_id, _fund_id, deposited, amount, _balance in rows:
        contacted.add(client_id)
        if deposited:
            deposited_clients.add(client_id)
            deposit_by_client[client_id] = max(deposit_by_client[client_id], float(amount or 0.0))

    totals = _client_totals(session, list(deposited_clients))
    stay_floor = get_settings().metrics_stay_balance_kes
    stayed = {
        client_id
        for client_id in deposited_clients
        if totals.get(client_id, (0.0, 0))[0] > stay_floor
    }
    money_kept = sum(deposit_by_client[client_id] for client_id in stayed)
    return LossesPreventedOut(
        window=window,
        at_risk_contacted=len(contacted),
        stayed_clients=len(stayed),
        money_kept_kes=money_kept,
    )


def growth_found_metric(session: Session, window: MetricWindow) -> GrowthFoundOut:
    rows = _risk_rows(session, window, OPPORTUNITY_KIND)
    deposit_by_client: dict[int, float] = defaultdict(float)
    deposited_clients: set[int] = set()
    introduced: set[int] = set()
    for client_id, _fund_id, deposited, amount, _balance in rows:
        introduced.add(client_id)
        if deposited:
            deposited_clients.add(client_id)
            deposit_by_client[client_id] = max(deposit_by_client[client_id], float(amount or 0.0))

    totals = _client_totals(session, list(deposited_clients))
    took_second = {
        client_id for client_id in deposited_clients if totals.get(client_id, (0.0, 0))[1] > 1
    }
    money_followed = sum(deposit_by_client[client_id] for client_id in took_second)
    return GrowthFoundOut(
        window=window,
        introduced_contacted=len(introduced),
        took_second_fund=len(took_second),
        money_followed_kes=money_followed,
    )


def quality_metric(session: Session, window: MetricWindow) -> QualityOut:
    start, end = _bounds(window)
    decisions = session.execute(
        select(ReviewAction.created_at, ReviewAction.outcome).where(
            ReviewAction.created_at >= start, ReviewAction.created_at < end
        )
    ).all()
    sends = session.scalars(
        select(TouchLog.sent_at)
        .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
        .join(Campaign, Campaign.campaign_id == Enrollment.campaign_id)
        .where(
            TouchLog.delivery_status == SENT_STATUS,
            TouchLog.sent_at.is_not(None),
            TouchLog.sent_at >= start,
            TouchLog.sent_at < end,
            Campaign.is_test.is_(False),
        )
    ).all()
    optouts = session.scalars(
        select(ContactEvent.occurred_at).where(
            ContactEvent.type == OPTOUT_EVENT,
            ContactEvent.occurred_at >= start,
            ContactEvent.occurred_at < end,
        )
    ).all()
    complaints = session.scalars(
        select(ClientComplaint.opened_at).where(
            ClientComplaint.opened_at >= window.since,
            ClientComplaint.opened_at <= window.until,
        )
    ).all()

    decisions_by_week: dict[date, list[str]] = defaultdict(list)
    for created_at, outcome in decisions:
        decisions_by_week[_week_start(_local_day(created_at))].append(outcome)
    sends_by_week: dict[date, int] = defaultdict(int)
    for sent_at in sends:
        sends_by_week[_week_start(_local_day(sent_at))] += 1
    optouts_by_week: dict[date, int] = defaultdict(int)
    for occurred_at in optouts:
        optouts_by_week[_week_start(_local_day(occurred_at))] += 1
    complaints_by_week: dict[date, int] = defaultdict(int)
    for opened_at in complaints:
        complaints_by_week[_week_start(opened_at)] += 1

    weeks = []
    for week_start in _week_range(window):
        outcomes = decisions_by_week.get(week_start, [])
        decided = len(outcomes)
        edits = sum(1 for outcome in outcomes if outcome == EDIT_OUTCOME)
        rejections = sum(1 for outcome in outcomes if outcome == REJECT_OUTCOME)
        sent = sends_by_week.get(week_start, 0)
        opt_outs = optouts_by_week.get(week_start, 0)
        weeks.append(
            QualityWeek(
                week_start=week_start,
                decisions=decided,
                edits=edits,
                rejections=rejections,
                edit_rate=_ratio(edits, decided),
                rejection_rate=_ratio(rejections, decided),
                sent=sent,
                opt_outs=opt_outs,
                opt_out_rate=_ratio(opt_outs, sent),
                complaints=complaints_by_week.get(week_start, 0),
            )
        )
    return QualityOut(window=window, weeks=weeks)


def usefulness_metric(session: Session, window: MetricWindow) -> UsefulnessOut:
    start, end = _bounds(window)
    rows = session.execute(
        select(AgentInsight.state, AgentInsight.dismissed_reason).where(
            AgentInsight.created_at >= start, AgentInsight.created_at < end
        )
    ).all()
    counts: dict[str, int] = defaultdict(int)
    reasons: dict[str, int] = defaultdict(int)
    for state, dismissed_reason in rows:
        counts[state] += 1
        if state == "dismissed":
            reasons[dismissed_reason or "no reason given"] += 1

    accepted = counts.get("accepted", 0)
    acted_on = counts.get("acted_on", 0)
    dismissed = counts.get("dismissed", 0)
    total = sum(counts.values())
    decided = accepted + acted_on + dismissed
    dismiss_reasons = [
        DismissReason(reason=reason, count=count)
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    ]
    return UsefulnessOut(
        window=window,
        total=total,
        accepted=accepted,
        acted_on=acted_on,
        dismissed=dismissed,
        new=counts.get("new", 0),
        expired=counts.get("expired", 0),
        acceptance_rate=_ratio(accepted + acted_on, decided),
        dismiss_reasons=dismiss_reasons,
    )


def freedom_metric(session: Session, window: MetricWindow) -> FreedomOut:
    start, end = _bounds(window)
    rows = session.execute(
        select(AgentProposal.created_at, AgentProposal.permission_applied).where(
            AgentProposal.created_at >= start, AgentProposal.created_at < end
        )
    ).all()
    by_week: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for created_at, permission in rows:
        by_week[_week_start(_local_day(created_at))][permission] += 1

    weeks = []
    for week_start in _week_range(window):
        levels = by_week.get(week_start, {})
        weeks.append(
            FreedomWeek(
                week_start=week_start,
                total=sum(levels.values()),
                suggest_only=levels.get("suggest_only", 0),
                approve_each=levels.get("approve_each", 0),
                approve_sample=levels.get("approve_sample", 0),
                act_alone=levels.get("act_alone", 0),
            )
        )
    return FreedomOut(window=window, weeks=weeks)


def cost_metric(session: Session, window: MetricWindow) -> CostOut:
    start, end = _bounds(window)
    finding_count = session.scalar(
        select(func.count())
        .select_from(AgentInsight)
        .where(AgentInsight.created_at >= start, AgentInsight.created_at < end)
    )
    discovery_cost = session.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_kes), 0.0)).where(
            AgentRun.agent_kind.in_(list(BOOK_WIDE_AGENTS)),
            AgentRun.started_at >= start,
            AgentRun.started_at < end,
        )
    )
    action_cost = session.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_kes), 0.0)).where(
            AgentRun.agent_kind == ACTION_AGENT,
            AgentRun.started_at >= start,
            AgentRun.started_at < end,
        )
    )
    send_spend = session.scalar(
        select(func.coalesce(func.sum(TouchLog.cost), 0)).where(
            TouchLog.delivery_status == SENT_STATUS,
            TouchLog.sent_at.is_not(None),
            TouchLog.sent_at >= start,
            TouchLog.sent_at < end,
        )
    )
    contacted_clients = session.scalar(
        select(func.count(func.distinct(ActionResult.client_id))).where(
            ActionResult.sent_at >= start, ActionResult.sent_at < end
        )
    )
    money_in = session.scalar(
        select(func.coalesce(func.sum(ActionResult.deposit_amount_kes), 0.0)).where(
            ActionResult.window_days == window.window_days,
            ActionResult.sent_at >= start,
            ActionResult.sent_at < end,
        )
    )

    discovery_cost = float(discovery_cost or 0.0)
    action_cost = float(action_cost or 0.0)
    send_spend = float(send_spend or 0.0)
    money_in = float(money_in or 0.0)
    total_cost = discovery_cost + action_cost + send_spend
    cost_per_finding = discovery_cost / finding_count if finding_count else None
    cost_per_contacted = (
        (action_cost + send_spend) / contacted_clients if contacted_clients else None
    )
    return_multiple = money_in / total_cost if total_cost > 0 else None
    return CostOut(
        window=window,
        finding_count=finding_count or 0,
        discovery_cost_kes=discovery_cost,
        cost_per_finding_kes=cost_per_finding,
        contacted_clients=contacted_clients or 0,
        action_cost_kes=action_cost,
        send_spend_kes=send_spend,
        cost_per_contacted_kes=cost_per_contacted,
        money_in_kes=money_in,
        total_cost_kes=total_cost,
        return_multiple=return_multiple,
    )


def system_speed_metric(session: Session, window: MetricWindow) -> SystemSpeedOut:
    start, end = _bounds(window)
    run_rows = session.execute(
        select(AgentRun.agent_kind, AgentRun.started_at, AgentRun.finished_at).where(
            AgentRun.finished_at.is_not(None),
            AgentRun.started_at >= start,
            AgentRun.started_at < end,
        )
    ).all()
    durations: dict[str, list[float]] = defaultdict(list)
    for agent_kind, started_at, finished_at in run_rows:
        durations[agent_kind].append((finished_at - started_at).total_seconds())

    runs = []
    for agent_kind in sorted(durations):
        values = durations[agent_kind]
        runs.append(
            RunDurationStat(
                agent_kind=agent_kind,
                runs=len(values),
                average_seconds=sum(values) / len(values) if values else None,
                median_seconds=_percentile(values, 0.5),
                p90_seconds=_percentile(values, 0.9),
            )
        )

    latencies = [
        float(value)
        for value in session.scalars(
            select(LLMResponse.latency_ms).where(
                LLMResponse.created_at >= start, LLMResponse.created_at < end
            )
        ).all()
    ]
    latency = LatencyStat(
        sample=len(latencies),
        p50_ms=_percentile(latencies, 0.5),
        p90_ms=_percentile(latencies, 0.9),
        p99_ms=_percentile(latencies, 0.99),
    )

    fund_count = session.scalar(select(func.count()).select_from(ActiveClientFund)) or 0
    client_count = (
        session.scalar(select(func.count(func.distinct(ActiveClientFund.client_id)))) or 0
    )
    return SystemSpeedOut(
        window=window,
        runs=runs,
        model_latency=latency,
        book=BookSize(active_client_funds=fund_count, active_clients=client_count),
    )


def dashboard(session: Session, window: MetricWindow) -> DashboardOut:
    return DashboardOut(
        window=window,
        speed=speed_metric(session, window),
        coverage=coverage_metric(session, window),
        money_at_risk=money_at_risk_metric(session, window),
        losses_prevented=losses_prevented_metric(session, window),
        growth_found=growth_found_metric(session, window),
        quality=quality_metric(session, window),
        usefulness=usefulness_metric(session, window),
        freedom=freedom_metric(session, window),
        cost=cost_metric(session, window),
        system_speed=system_speed_metric(session, window),
    )
