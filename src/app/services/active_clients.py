"""Console reads and writes for the active-client (risk/digest) population.

Everything here is keyed by (client_id, unit_fund_id), the active book's own
key -- distinct from the dormant client_fund population app.services.clients
covers. The active-client population has no campaign, enrollment, or
outreach_message path in this codebase yet: the interaction log below is
manual FA bookkeeping, never a send trigger. No query here reads pii_vault;
a name is never re-attached on any of these reads.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Date, func, select, tuple_
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveTransaction,
)
from app.db.models.audit import AuditLog
from app.db.models.complaints import ClientComplaint
from app.db.models.models import Funds
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion, RiskSnapshot
from app.pagination import (
    DEFAULT_LIMIT,
    clamp_limit,
    decode_cursor,
    decode_id_cursor,
    decode_pair_cursor,
    encode_cursor,
    encode_id_cursor,
    encode_pair_cursor,
)
from app.risk.magnitude import primary_signal_magnitude
from app.risk.routing import route_direction
from app.risk.signals import SIGNAL_ORDER, fired_signal_tags
from app.services.briefing import briefing_available_keys


class ActiveClientNotFound(Exception):
    """No active_client_fund row for this client-fund key."""


def _fund_name(session: Session, unit_fund_id: int) -> str:
    name = session.scalar(select(Funds.unit_fund_name).where(Funds.unit_fund_id == unit_fund_id))
    return name if name is not None else f"Fund {unit_fund_id}"


def record_interaction(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    type: str,
    note: str | None,
    reviewer_id: str,
) -> ActiveClientInteraction:
    """Log one FA action against a client-fund, or raise ActiveClientNotFound.

    reviewer_id is the caller X-Reviewer-Key resolved to (see
    app.api.reviewer_auth), never a self-reported field on the request
    body. Audited the same as every other write path in this codebase.
    """
    if session.get(ActiveClientFund, (client_id, unit_fund_id)) is None:
        raise ActiveClientNotFound(f"{client_id}/{unit_fund_id}")

    row = ActiveClientInteraction(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        type=type,
        note=note,
        reviewer_id=reviewer_id,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        entity_type="active_client_interaction",
        action=type,
        entity_id=f"{client_id}/{unit_fund_id}",
        actor_id=reviewer_id,
        detail={"note": note} if note else None,
    )
    session.commit()
    return row


def list_interactions(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    since: datetime | None = None,
) -> list[ActiveClientInteraction]:
    """This client-fund's logged interactions, most recent first."""
    query = select(ActiveClientInteraction).where(
        ActiveClientInteraction.client_id == client_id,
        ActiveClientInteraction.unit_fund_id == unit_fund_id,
    )
    if since is not None:
        query = query.where(ActiveClientInteraction.created_at >= since)
    query = query.order_by(ActiveClientInteraction.created_at.desc())
    return list(session.scalars(query).all())


def list_transactions(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    limit: int | None = None,
) -> list[ActiveTransaction]:
    """This client-fund's observed deposits and withdrawals, most recent
    first, a row with no parsed date last rather than dropped. Accumulated
    across every nightly transform run -- see
    app.db.models.active_clients.ActiveTransaction -- so this can hold more
    than the feed's own "last 5 purchases" / "last 2 sales" per-pull cap,
    though never a claim of full lifetime history: see
    ActiveClientFund.deposit_count_capped / withdrawal_history_hidden.
    """
    query = (
        select(ActiveTransaction)
        .where(
            ActiveTransaction.client_id == client_id,
            ActiveTransaction.unit_fund_id == unit_fund_id,
        )
        .order_by(ActiveTransaction.txn_date.desc().nullslast(), ActiveTransaction.txn_id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query).all())


@dataclass(frozen=True)
class ActiveClientProfile:
    """Every non-PII fact this codebase holds about one active-client-fund
    relationship, gathered from the tables get_active_client_profile is
    allowed to read (see module docstring).
    """

    active: ActiveClientFund
    risk: ClientRiskFeatures | None
    fund_name: str
    primary_signal_magnitude: str | None
    risk_history: list[RiskSnapshot]
    complaints: list[ClientComplaint]
    interactions: list[ActiveClientInteraction]
    transactions: list[ActiveTransaction]


def _signals_dict(risk: ClientRiskFeatures) -> dict[str, bool]:
    return {name: getattr(risk, name) for name in SIGNAL_ORDER}


def _config_weights(session: Session, config_version: int) -> dict[str, float]:
    weights = session.scalar(
        select(RiskConfigVersion.weights).where(RiskConfigVersion.version == config_version)
    )
    return weights or {}


def get_active_client_profile(
    session: Session, client_id: int, unit_fund_id: int
) -> ActiveClientProfile:
    """The fuller active-client profile: identity, current bands, risk
    history, and complaint/interaction/transaction history. Raises
    ActiveClientNotFound when there is no active_client_fund row at all; a
    client_risk_features row missing (no nightly run has scored it yet) is
    not that -- bands come back null instead.
    """
    active = session.get(ActiveClientFund, (client_id, unit_fund_id))
    if active is None:
        raise ActiveClientNotFound(f"{client_id}/{unit_fund_id}")

    risk = session.get(ClientRiskFeatures, (client_id, unit_fund_id))
    psm = None
    if risk is not None:
        psm = primary_signal_magnitude(
            signals=_signals_dict(risk),
            weights=_config_weights(session, risk.config_version),
            last_deposit=active.last_deposit_date,
            overdue_multiple=risk.overdue_multiple,
            largest_withdrawal=active.largest_withdrawal,
            balance=active.balance,
            deposit_trend=active.deposit_trend,
            months_until_empty=active.months_until_empty,
        )

    risk_history = list(
        session.scalars(
            select(RiskSnapshot)
            .where(RiskSnapshot.client_id == client_id, RiskSnapshot.unit_fund_id == unit_fund_id)
            .order_by(RiskSnapshot.snapshot_id.desc())
        )
    )
    complaints = list(
        session.scalars(
            select(ClientComplaint)
            .where(ClientComplaint.client_id == client_id)
            .order_by(ClientComplaint.opened_at.desc())
        )
    )

    return ActiveClientProfile(
        active=active,
        risk=risk,
        fund_name=_fund_name(session, unit_fund_id),
        primary_signal_magnitude=psm,
        risk_history=risk_history,
        complaints=complaints,
        interactions=list_interactions(session, client_id, unit_fund_id),
        transactions=list_transactions(session, client_id, unit_fund_id),
    )


@dataclass(frozen=True)
class DepositPercentile:
    """Where one client-fund's observed lifetime deposit total ranks
    against the whole active book's.
    """

    total_deposits: float
    book_size: int
    rank: int
    percentile: float | None
    deposit_count_capped: bool


def deposit_percentile(session: Session, client_id: int, unit_fund_id: int) -> DepositPercentile:
    """Rank this client-fund's observed deposit total against every
    active_client_fund row -- the same "book" book_coverage() uses.

    "Observed" deposit total, summed from active_transaction, not a claim
    of full lifetime history -- deposit_count_capped carries the same
    caveat ActiveClientIdentityOut already attaches to the raw transaction
    list. Raises ActiveClientNotFound when there is no active_client_fund
    row. No SQL window function: a book-wide count-below/count-above
    comparison, the same plain-aggregate idiom every other query in this
    module uses.
    """
    active = session.get(ActiveClientFund, (client_id, unit_fund_id))
    if active is None:
        raise ActiveClientNotFound(f"{client_id}/{unit_fund_id}")

    totals = (
        select(
            ActiveTransaction.client_id,
            ActiveTransaction.unit_fund_id,
            func.sum(ActiveTransaction.amount).label("total"),
        )
        .where(ActiveTransaction.txn_type == "purchase")
        .group_by(ActiveTransaction.client_id, ActiveTransaction.unit_fund_id)
        .subquery()
    )
    book = (
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            func.coalesce(totals.c.total, 0.0).label("total"),
        )
        .select_from(ActiveClientFund)
        .outerjoin(
            totals,
            (totals.c.client_id == ActiveClientFund.client_id)
            & (totals.c.unit_fund_id == ActiveClientFund.unit_fund_id),
        )
        .subquery()
    )

    own_total = (
        session.scalar(
            select(book.c.total).where(
                book.c.client_id == client_id, book.c.unit_fund_id == unit_fund_id
            )
        )
        or 0.0
    )
    book_size = session.scalar(select(func.count()).select_from(book)) or 0
    above = (
        session.scalar(select(func.count()).select_from(book).where(book.c.total > own_total)) or 0
    )
    below = (
        session.scalar(select(func.count()).select_from(book).where(book.c.total < own_total)) or 0
    )
    percentile = round(100.0 * below / (book_size - 1), 1) if book_size > 1 else None

    return DepositPercentile(
        total_deposits=own_total,
        book_size=book_size,
        rank=above + 1,
        percentile=percentile,
        deposit_count_capped=active.deposit_count_capped,
    )


def list_active_roster(
    session: Session,
    *,
    client_id: int | None = None,
    risk_band: str | None = None,
    route: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
):
    """Every active_client_fund row, left-joined to its current risk row,
    keyset-paginated on (client_id, unit_fund_id) -- the same composite
    cursor app.services.risk.list_small_balance_review_queue already uses. A
    client-fund with no client_risk_features row yet (unscored) still
    appears here, with null risk fields, not a gap in the roster.

    client_id, when given, is an exact match against the same "Find by ID"
    box app.services.clients.list_clients already exposes on the dormant
    roster. risk_reason_tags is computed the same way
    ActiveClientBandsOut.risk_reason_tags is, via
    app.risk.signals.fired_signal_tags -- empty for an unscored row rather
    than a gap. primary_signal_magnitude is the same computation
    ActiveClientBandsOut.primary_signal_magnitude uses, via
    app.risk.magnitude.primary_signal_magnitude. fund_name,
    complaint_caveat, last_interaction_at, and briefing_available are
    looked up in extra queries scoped to just this page's rows, not the
    whole book, so the roster stays one page-sized read regardless of how
    big the book gets.
    """
    limit = clamp_limit(limit)
    query = (
        select(
            ActiveClientFund.client_id,
            ActiveClientFund.unit_fund_id,
            ActiveClientFund.client_code,
            ActiveClientFund.balance,
            ActiveClientFund.last_deposit_date,
            ActiveClientFund.largest_withdrawal,
            ActiveClientFund.deposit_trend,
            ActiveClientFund.months_until_empty,
            ClientRiskFeatures.risk_band,
            ClientRiskFeatures.risk_score,
            ClientRiskFeatures.fund_at_risk,
            ClientRiskFeatures.route,
            ClientRiskFeatures.overdue_multiple,
            ClientRiskFeatures.sig_broken_pattern,
            ClientRiskFeatures.sig_dormant,
            ClientRiskFeatures.sig_heavy_withdrawal,
            ClientRiskFeatures.sig_shrinking,
            ClientRiskFeatures.sig_going_dormant,
            ClientRiskFeatures.sig_never_repeated,
            RiskConfigVersion.weights,
        )
        .select_from(ActiveClientFund)
        .join(
            ClientRiskFeatures,
            (ClientRiskFeatures.client_id == ActiveClientFund.client_id)
            & (ClientRiskFeatures.unit_fund_id == ActiveClientFund.unit_fund_id),
            isouter=True,
        )
        .join(
            RiskConfigVersion,
            RiskConfigVersion.version == ClientRiskFeatures.config_version,
            isouter=True,
        )
    )
    if client_id is not None:
        query = query.where(ActiveClientFund.client_id == client_id)
    if risk_band is not None:
        query = query.where(ClientRiskFeatures.risk_band == risk_band)
    if route is not None:
        query = query.where(ClientRiskFeatures.route == route)
    if cursor is not None:
        after_client_id, after_unit_fund_id = decode_pair_cursor(cursor)
        query = query.where(
            tuple_(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)
            > (after_client_id, after_unit_fund_id)
        )
    query = query.order_by(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id).limit(
        limit + 1
    )

    rows = list(session.execute(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_pair_cursor(rows[-1].client_id, rows[-1].unit_fund_id)
    if not rows:
        return [], next_cursor

    page_client_ids = {r.client_id for r in rows}
    page_fund_ids = {r.unit_fund_id for r in rows}
    page_keys = [(r.client_id, r.unit_fund_id) for r in rows]
    open_complaint_ids = set(
        session.scalars(
            select(ClientComplaint.client_id).where(
                ClientComplaint.client_id.in_(page_client_ids), ClientComplaint.status == "open"
            )
        )
    )
    last_interaction = {
        (r.client_id, r.unit_fund_id): r.last_at
        for r in session.execute(
            select(
                ActiveClientInteraction.client_id,
                ActiveClientInteraction.unit_fund_id,
                func.max(ActiveClientInteraction.created_at).label("last_at"),
            )
            .where(
                tuple_(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id).in_(
                    page_keys
                )
            )
            .group_by(ActiveClientInteraction.client_id, ActiveClientInteraction.unit_fund_id)
        ).all()
    }
    fund_names = dict(
        session.execute(
            select(Funds.unit_fund_id, Funds.unit_fund_name).where(
                Funds.unit_fund_id.in_(page_fund_ids)
            )
        ).all()
    )
    briefing_available = briefing_available_keys(session, page_keys)

    items = [
        {
            "client_id": r.client_id,
            "unit_fund_id": r.unit_fund_id,
            "client_code": r.client_code,
            "fund_name": fund_names.get(r.unit_fund_id, f"Fund {r.unit_fund_id}"),
            "balance": r.balance,
            "risk_band": r.risk_band,
            "risk_score": r.risk_score,
            "fund_at_risk": r.fund_at_risk,
            "route": r.route,
            "primary_signal_magnitude": primary_signal_magnitude(
                signals={name: getattr(r, name) for name in SIGNAL_ORDER},
                weights=r.weights or {},
                last_deposit=r.last_deposit_date,
                overdue_multiple=r.overdue_multiple,
                largest_withdrawal=r.largest_withdrawal,
                balance=r.balance,
                deposit_trend=r.deposit_trend,
                months_until_empty=r.months_until_empty,
            ),
            "briefing_available": (r.client_id, r.unit_fund_id) in briefing_available,
            "risk_reason_tags": fired_signal_tags(r),
            "complaint_caveat": r.client_id in open_complaint_ids,
            "last_interaction_at": last_interaction.get((r.client_id, r.unit_fund_id)),
        }
        for r in rows
    ]
    return items, next_cursor


def _direction_counts(session: Session, run_ids: list[str]) -> dict[str, tuple[int, int]]:
    """(more_urgent_count, less_urgent_count) per run_id, from each run's
    "risk_snapshot"/"route" audit_log entry -- the one that actually carries
    the per-client from_route/to_route pairs, a different entry from the
    "risk_run"/"complete" one list_route_change_history reads for its other
    fields. A run with no such entry (nothing changed, or it predates
    from_route being recorded) contributes (0, 0).
    """
    if not run_ids:
        return {}
    rows = session.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "risk_snapshot",
            AuditLog.action == "route",
            AuditLog.run_id.in_(run_ids),
        )
    ).all()
    counts: dict[str, tuple[int, int]] = {}
    for row in rows:
        changed = (row.detail or {}).get("changed", [])
        more_urgent = sum(
            1 for c in changed if route_direction(c.get("from_route"), c["route"]) == "more_urgent"
        )
        less_urgent = sum(
            1 for c in changed if route_direction(c.get("from_route"), c["route"]) == "less_urgent"
        )
        counts[row.run_id] = (more_urgent, less_urgent)
    return counts


def list_route_change_history(
    session: Session, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> tuple[list[dict], str | None]:
    """Route-churn summary per completed nightly risk run, newest first.

    Reads app.workers.risk_detection's own "risk_run"/"complete" audit_log
    entry -- the same routes_changed and route_distribution figures the
    worker itself logs -- rather than a dedicated table. more_urgent_count/
    less_urgent_count come from a second, per-run lookup (see
    _direction_counts) rather than this entry, which never carried them. No
    schema change either way, since audit_log is already the durable record.
    """
    limit = clamp_limit(limit)
    query = select(AuditLog).where(
        AuditLog.entity_type == "risk_run", AuditLog.action == "complete"
    )
    if cursor is not None:
        before_created_at, before_id = decode_cursor(cursor)
        query = query.where(
            tuple_(AuditLog.created_at, AuditLog.log_id) < (before_created_at, int(before_id))
        )
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.log_id.desc()).limit(limit + 1)
    rows = list(session.scalars(query).all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, str(last.log_id))

    direction_counts = _direction_counts(session, [row.run_id for row in rows if row.run_id])

    items = [
        {
            "run_id": row.run_id,
            "as_of": row.created_at,
            "clients_seen": (row.detail or {}).get("clients_seen", 0),
            "routes_changed": (row.detail or {}).get("routes_changed", 0),
            "route_distribution": (row.detail or {}).get("route_distribution", {}),
            "more_urgent_count": direction_counts.get(row.run_id, (0, 0))[0],
            "less_urgent_count": direction_counts.get(row.run_id, (0, 0))[1],
        }
        for row in rows
    ]
    return items, next_cursor


def route_change_details(
    session: Session, *, run_id: str | None = None, cursor: str | None = None, limit: int = 10
) -> tuple[str | None, datetime | None, list[dict], str | None]:
    """The individual client route moves behind one nightly run's
    routes_changed count -- the latest completed run with any changes by
    default, or a specific run_id.

    Reads the same "risk_snapshot"/"route" audit_log entry the worker
    writes when changes is non-empty. That entry's "changed" list is fixed
    once written, so it's paginated in memory with a plain offset cursor
    (encode_id_cursor/decode_id_cursor) -- safe here even though the rest of
    this module avoids offset pagination for live, growing queries.
    """
    limit = clamp_limit(limit)
    query = select(AuditLog).where(
        AuditLog.entity_type == "risk_snapshot", AuditLog.action == "route"
    )
    if run_id is not None:
        query = query.where(AuditLog.run_id == run_id)
    query = query.order_by(AuditLog.created_at.desc(), AuditLog.log_id.desc()).limit(1)
    row = session.scalars(query).first()
    if row is None:
        return run_id, None, [], None

    changed = (row.detail or {}).get("changed", [])
    offset = decode_id_cursor(cursor) if cursor is not None else 0
    page = changed[offset : offset + limit]
    next_cursor = encode_id_cursor(offset + limit) if offset + limit < len(changed) else None
    if not page:
        return row.run_id, row.created_at, [], None

    page_client_ids = {c["client_id"] for c in page}
    page_fund_ids = {c["unit_fund_id"] for c in page}
    client_codes = dict(
        session.execute(
            select(ActiveClientFund.client_id, ActiveClientFund.client_code).where(
                ActiveClientFund.client_id.in_(page_client_ids)
            )
        ).all()
    )
    fund_names = dict(
        session.execute(
            select(Funds.unit_fund_id, Funds.unit_fund_name).where(
                Funds.unit_fund_id.in_(page_fund_ids)
            )
        ).all()
    )

    items = [
        {
            "client_id": c["client_id"],
            "unit_fund_id": c["unit_fund_id"],
            "client_code": client_codes.get(c["client_id"]),
            "fund_name": fund_names.get(c["unit_fund_id"], f"Fund {c['unit_fund_id']}"),
            "from_route": c.get("from_route"),
            "to_route": c["route"],
            "direction": route_direction(c.get("from_route"), c["route"]),
            "from_risk_band": c.get("from_risk_band"),
            "risk_band": c["risk_band"],
            "reasons": c.get("reasons", ""),
        }
        for c in page
    ]
    return row.run_id, row.created_at, items, next_cursor


def _months_ago(reference: date, months: int) -> date:
    """reference's own day-of-month, `months` calendar months earlier,
    clamped to the shorter month's last day when the day doesn't exist
    there (e.g. March 31 minus one month is not Feb 31).
    """
    month_index = reference.month - 1 - months
    year = reference.year + month_index // 12
    month = month_index % 12 + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class TransactionAnalytics:
    """Book-wide transaction patterns over the trailing window: deposit and
    withdrawal volume by month, and a breakdown of sale_type among
    withdrawals -- the only two cuts active_transaction supports without a
    client-fund scope.
    """

    by_month: list[tuple[date, str, int, float, float | None]]
    by_sale_type: list[tuple[str | None, int, float]]


def transaction_analytics(session: Session, *, months: int = 12) -> TransactionAnalytics:
    """Book-wide deposit/withdrawal volume by month (trailing `months`
    months) and a sale_type breakdown among withdrawals, both read from
    active_transaction -- the accumulated observed history, same table
    deposit_percentile reads, not the feed's own capped
    last_5_purchases/last_2_sales window.

    Rows with no txn_date (an ingestion gap, see ActiveTransaction's own
    docstring) are excluded from by_month, since they cannot be placed on
    the timeline, but nothing here raises on them.
    """
    cutoff = _months_ago(date.today(), months)
    month_expr = func.cast(func.date_trunc("month", ActiveTransaction.txn_date), Date).label(
        "month"
    )

    by_month = list(
        session.execute(
            select(
                month_expr,
                ActiveTransaction.txn_type,
                func.count(),
                func.coalesce(func.sum(ActiveTransaction.amount), 0.0),
                func.avg(ActiveTransaction.fees_incurred),
            )
            .where(ActiveTransaction.txn_date.is_not(None), ActiveTransaction.txn_date >= cutoff)
            .group_by(month_expr, ActiveTransaction.txn_type)
            .order_by(month_expr)
        ).all()
    )

    by_sale_type = list(
        session.execute(
            select(
                ActiveTransaction.sale_type,
                func.count(),
                func.coalesce(func.sum(ActiveTransaction.amount), 0.0),
            )
            .where(ActiveTransaction.txn_type == "sale")
            .group_by(ActiveTransaction.sale_type)
            .order_by(func.count().desc())
        ).all()
    )

    return TransactionAnalytics(by_month=by_month, by_sale_type=by_sale_type)
