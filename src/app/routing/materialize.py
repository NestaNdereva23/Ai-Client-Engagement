from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.models import ClientFeatures, ClientFund, Clients, Funds
from app.routing.sides import CAPITAL_PRESERVATION_KES
from app.transform.features import (
    derive_features,
    derive_relationship_measures,
    largest_first,
    relationships_by_client,
)
from app.transform.flatten import ClientRow, FlattenResult, FundRow, TxnRow
from app.transform.load import (
    _CLIENT_FUND_UPDATE,
    _CLIENT_UPDATE,
    _FEATURE_UPDATE,
    _FUND_UPDATE,
    _client_dict,
    _client_fund_dict,
    _feature_dict,
    upsert,
)

logger = structlog.get_logger(__name__)


def _reclassified_ids(session: Session, threshold: float):
    return (
        select(ActiveClientFund.client_id)
        .group_by(ActiveClientFund.client_id)
        .having(func.coalesce(func.sum(ActiveClientFund.balance), 0.0) < threshold)
        .subquery()
    )


def _fund_names(
    session: Session, fund_ids: set[int], txns: list[ActiveTransaction]
) -> dict[int, str]:
    names: dict[int, str] = {}
    for txn in txns:
        if txn.fund_short_name and txn.unit_fund_id not in names:
            names[txn.unit_fund_id] = txn.fund_short_name
    missing = [fid for fid in fund_ids if fid not in names]
    if missing:
        for fund in session.scalars(select(Funds).where(Funds.unit_fund_id.in_(missing))):
            names[fund.unit_fund_id] = fund.unit_fund_name
    for fid in fund_ids:
        names.setdefault(fid, f"Fund {fid}")
    return names


def _txn_row(txn: ActiveTransaction) -> TxnRow:
    return TxnRow(
        txn_id=txn.txn_id,
        txn_type=txn.txn_type,
        client_id=txn.client_id,
        client_code=None,
        unit_fund_id=txn.unit_fund_id,
        fund_short_name=txn.fund_short_name,
        date=txn.txn_date,
        amount=txn.amount,
        unit_price=txn.unit_price,
        fees_incurred=txn.fees_incurred,
        sale_type=txn.sale_type,
    )


def _client_row(
    fund: ActiveClientFund,
    deposit_total: float,
    sale_total: float,
    reference: datetime,
) -> ClientRow:
    last_activity = max(
        (d for d in (fund.last_deposit_date, fund.last_withdrawal_slot_date) if d is not None),
        default=None,
    )
    days_since = (reference.date() - last_activity).days if last_activity is not None else None
    return ClientRow(
        client_id=fund.client_id,
        client_code=fund.client_code,
        client_name=None,
        client_email=None,
        client_phone=None,
        unit_fund_id=fund.unit_fund_id,
        balance=fund.balance,
        n_purchases_returned=fund.n_deposits,
        n_sales_returned=fund.n_withdrawals,
        last_purchase_date=fund.last_deposit_date,
        last_sale_date=fund.last_withdrawal_slot_date,
        total_purchase_amount=deposit_total,
        total_sale_amount=sale_total,
        last_activity_date=last_activity,
        days_since_last_activity=days_since,
        computed_at=fund.computed_at,
        purchases_censored=fund.deposit_count_capped,
        history_censored=fund.deposit_count_capped or fund.withdrawal_history_hidden,
        has_extended_history=False,
        activity_window_from=None,
        activity_window_to=None,
        fa_name=fund.fa_name,
        fa_email=fund.fa_email,
    )


def _build_result(
    acf_rows: list[ActiveClientFund],
    txns: list[ActiveTransaction],
    fund_names: dict[int, str],
    reference: datetime,
) -> FlattenResult:
    deposit_totals: dict[tuple[int, int], float] = {}
    sale_totals: dict[tuple[int, int], float] = {}
    for txn in txns:
        key = (txn.client_id, txn.unit_fund_id)
        if txn.txn_type == "purchase":
            deposit_totals[key] = deposit_totals.get(key, 0.0) + txn.amount
        else:
            sale_totals[key] = sale_totals.get(key, 0.0) + txn.amount

    result = FlattenResult()
    result.funds = [
        FundRow(unit_fund_id=fid, unit_fund_name=name, inactive_client_count=None)
        for fid, name in fund_names.items()
    ]
    result.transactions = [_txn_row(t) for t in txns]
    for fund in acf_rows:
        key = (fund.client_id, fund.unit_fund_id)
        result.clients.append(
            _client_row(
                fund,
                deposit_totals.get(key, 0.0),
                sale_totals.get(key, 0.0),
                reference,
            )
        )
    return result


def materialize_inactive_from_active(
    session: Session,
    *,
    reference: datetime,
    threshold: float = CAPITAL_PRESERVATION_KES,
) -> int:
    reclassified = _reclassified_ids(session, threshold)
    acf_rows = list(
        session.scalars(
            select(ActiveClientFund).where(
                ActiveClientFund.client_id.in_(select(reclassified.c.client_id))
            )
        )
    )
    if not acf_rows:
        return 0

    txns = list(
        session.scalars(
            select(ActiveTransaction).where(
                ActiveTransaction.client_id.in_(select(reclassified.c.client_id))
            )
        )
    )
    fund_ids = {fund.unit_fund_id for fund in acf_rows}
    fund_names = _fund_names(session, fund_ids, txns)

    result = _build_result(acf_rows, txns, fund_names, reference)
    measures = derive_relationship_measures(result)
    by_client = relationships_by_client(result)
    features = derive_features(result, measures)

    clients: list[dict] = []
    client_funds: list[dict] = []
    for rows in by_client.values():
        ordered = largest_first(rows)
        primary = ordered[0]
        clients.append(_client_dict(primary, n_funds=len(ordered)))
        client_funds.extend(
            _client_fund_dict(
                row, measures[(row.client_id, row.unit_fund_id)], is_primary=row is primary
            )
            for row in ordered
        )
    feature_rows = [_feature_dict(f) for f in features]
    fund_rows = [
        {"unit_fund_id": fid, "unit_fund_name": name, "inactive_client_count": None}
        for fid, name in fund_names.items()
    ]

    upsert(
        session,
        Funds,
        fund_rows,
        "unit_fund_id",
        _FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    upsert(session, Clients, clients, "client_id", _CLIENT_UPDATE)
    upsert(
        session,
        ClientFund,
        client_funds,
        ("client_id", "unit_fund_id"),
        _CLIENT_FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    upsert(
        session,
        ClientFeatures,
        feature_rows,
        "client_id",
        _FEATURE_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    session.commit()
    logger.info(
        "routing.materialized_inactive", clients=len(clients), relationships=len(client_funds)
    )
    return len(clients)
