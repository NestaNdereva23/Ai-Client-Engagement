"""Flatten the active-clients payload into funds, clients, and transactions.

Same shape and quirks as transform/flatten.py, reusing its date and amount
parsing so both feeds treat mixed ISO8601 dates and string amounts the same
way. Two things differ: there is no "collapse to one primary contact" step,
since active_client_fund keeps one row per client-fund relationship rather
than one row per person, and sale rows carry sale_type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.models import IngestionStatus, RawStaging
from app.ingestion.contracts_active import ActiveClientRecord, ActiveFundRecord
from app.transform.flatten import (
    PURCHASE_CAP,
    SALE_CAP,
    FlattenCounters,
    max_date,
    parse_amount,
    parse_date,
)


@dataclass
class ActiveClientRow:
    client_id: int
    client_code: int | str | None
    client_name: str | None
    unit_fund_id: int
    balance: float | None
    n_purchases: int
    n_sales: int
    last_purchase: date | None
    last_sale: date | None
    purchases_censored: bool
    redemption_history_blind: bool
    computed_at: str | None


@dataclass
class ActiveTxnRow:
    txn_id: int | None
    txn_type: str
    client_id: int
    unit_fund_id: int | None
    fund_short_name: str | None
    date: date | None
    amount: float
    unit_price: float | None
    fees_incurred: float | None
    sale_type: str | None


@dataclass
class ActiveFlattenResult:
    clients: list[ActiveClientRow] = field(default_factory=list)
    transactions: list[ActiveTxnRow] = field(default_factory=list)
    counters: FlattenCounters = field(default_factory=FlattenCounters)


def _active_txn_row(
    txn: dict[str, Any],
    client: ActiveClientRecord,
    fund_id: int,
    txn_type: str,
    counters: FlattenCounters,
) -> ActiveTxnRow:
    embedded = txn.get("unit_fund") or txn.get("fund") or {}
    return ActiveTxnRow(
        txn_id=txn.get("id"),
        txn_type=txn_type,
        client_id=client.client_id,
        unit_fund_id=txn.get("unit_fund_id", fund_id),
        fund_short_name=embedded.get("short_name"),
        date=parse_date(txn.get("date"), counters),
        amount=parse_amount(txn.get("number"), counters),
        unit_price=txn.get("unit_price"),
        fees_incurred=txn.get("fees_incurred"),
        sale_type=txn.get("sale_type"),
    )


def flatten_active_payload(
    payload: dict[str, Any], reference_date: datetime
) -> ActiveFlattenResult:
    """Flatten one active-clients raw payload into clients and transactions.

    reference_date is accepted for symmetry with flatten_payload, though the
    active-book row itself carries no days_since_* field yet; the active-book
    feature derivation milestone is what needs the anchor.
    """
    result = ActiveFlattenResult()

    env_data = payload.get("data", []) or []
    for fund_raw in env_data:
        try:
            fund = ActiveFundRecord.model_validate(fund_raw)
        except ValidationError:
            result.counters.funds_skipped += 1
            continue

        for client_raw in fund.clients:
            try:
                client = ActiveClientRecord.model_validate(client_raw)
            except ValidationError:
                result.counters.clients_skipped += 1
                continue

            purchases = client_raw.get("last_5_purchases") or []
            sales = client_raw.get("last_2_sales") or []
            purchase_rows = [
                _active_txn_row(t, client, fund.unit_fund_id, "purchase", result.counters)
                for t in purchases
            ]
            sale_rows = [
                _active_txn_row(t, client, fund.unit_fund_id, "sale", result.counters)
                for t in sales
            ]
            result.transactions.extend(purchase_rows)
            result.transactions.extend(sale_rows)

            last_purchase = max_date([r.date for r in purchase_rows])
            last_sale = max_date([r.date for r in sale_rows])

            purchases_censored = len(purchases) >= PURCHASE_CAP
            redemption_history_blind = len(sales) >= SALE_CAP

            result.clients.append(
                ActiveClientRow(
                    client_id=client.client_id,
                    client_code=client.client_code,
                    client_name=client.client_name,
                    unit_fund_id=fund.unit_fund_id,
                    balance=client.balance,
                    n_purchases=len(purchases),
                    n_sales=len(sales),
                    last_purchase=last_purchase,
                    last_sale=last_sale,
                    purchases_censored=purchases_censored,
                    redemption_history_blind=redemption_history_blind,
                    computed_at=client.computed_at,
                )
            )

    return result


def _load_reference_ts(session: Session, run_id: str) -> datetime:
    ref = session.execute(
        select(IngestionStatus.reference_ts).where(IngestionStatus.run_id == run_id)
    ).scalar_one_or_none()
    if ref is None:
        raise ValueError(f"run {run_id} has no persisted reference_ts to anchor days_since_*")
    return ref


def flatten_active_run(
    session: Session, run_id: str, reference_date: datetime | None = None
) -> ActiveFlattenResult:
    """Read a run's raw staging pages and flatten them into one result."""
    ref = reference_date if reference_date is not None else _load_reference_ts(session, run_id)
    payloads = (
        session.execute(
            select(RawStaging.payload)
            .where(RawStaging.run_id == run_id)
            .order_by(RawStaging.natural_key)
        )
        .scalars()
        .all()
    )

    combined = ActiveFlattenResult()
    for payload in payloads:
        page = flatten_active_payload(payload, reference_date=ref)
        combined.clients.extend(page.clients)
        combined.transactions.extend(page.transactions)
        combined.counters.merge(page.counters)

    return combined
