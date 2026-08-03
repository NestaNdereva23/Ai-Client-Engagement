"""Flatten the nested client payload into funds, clients, and transactions.

The source nests clients under funds and transactions under clients, with the
fund record repeated inside every transaction. This turns one payload into three
flat lists and derives each client's activity fields (last dates, totals, days
since last activity).

One client row is produced per client and fund, since a client holding two funds
appears once under each with a separate history. Collapsing them to one person
happens later, when deciding who to contact.

It reads from raw staging, never the source, so re-processing is free. Amounts
arrive as strings and dates in mixed ISO formats; both are parsed leniently and
anything unparseable is counted rather than raising.

days_since_* is anchored to a reference timestamp, not the wall clock. flatten_run
reads that timestamp back from the run's ingestion_status row, so re-running the
same run gives identical derivations. The timestamp is stored and read over an
EAT connection, so its calendar date is already East Africa Time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.models import IngestionStatus, RawStaging
from app.ingestion.contracts import ClientRecord, FundRecord, RawEnvelope

# The source returns at most this many purchases and sales per client. Hitting a
# cap means real history is hidden behind it.
PURCHASE_CAP = 5
SALE_CAP = 2


@dataclass
class FundRow:
    unit_fund_id: int
    unit_fund_name: str | None
    inactive_client_count: int | None


@dataclass
class ClientRow:
    client_id: int
    client_code: int | str | None
    client_name: str | None
    unit_fund_id: int
    balance: float | None
    n_purchases_returned: int
    n_sales_returned: int
    last_purchase_date: date | None
    last_sale_date: date | None
    total_purchase_amount: float
    total_sale_amount: float
    last_activity_date: date | None
    days_since_last_activity: int | None
    computed_at: str | None
    purchases_censored: bool
    history_censored: bool


@dataclass
class TxnRow:
    txn_id: int | None
    txn_type: str
    client_id: int
    client_code: int | str | None
    unit_fund_id: int | None
    fund_short_name: str | None
    date: date | None
    amount: float
    unit_price: float | None
    fees_incurred: float | None
    sale_type: str | None


@dataclass
class FlattenCounters:
    """Data quality signals collected while flattening, not errors."""

    funds_skipped: int = 0
    clients_skipped: int = 0
    dates_unparsed: int = 0
    amounts_unparsed: int = 0

    def merge(self, other: FlattenCounters) -> None:
        self.funds_skipped += other.funds_skipped
        self.clients_skipped += other.clients_skipped
        self.dates_unparsed += other.dates_unparsed
        self.amounts_unparsed += other.amounts_unparsed


@dataclass
class FlattenResult:
    funds: list[FundRow] = field(default_factory=list)
    clients: list[ClientRow] = field(default_factory=list)
    transactions: list[TxnRow] = field(default_factory=list)
    counters: FlattenCounters = field(default_factory=FlattenCounters)


def _parse_date(value: Any, counters: FlattenCounters) -> date | None:
    """Parse a mixed ISO date to a calendar date, counting anything unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        counters.dates_unparsed += 1
        return None


def _parse_amount(value: Any, counters: FlattenCounters) -> float:
    """Parse a string amount to a float, counting anything unparseable as zero."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        counters.amounts_unparsed += 1
        return 0.0


def _max_date(dates: list[date | None]) -> date | None:
    present = [d for d in dates if d is not None]
    return max(present) if present else None


def _txn_row(
    txn: dict[str, Any],
    client: ClientRecord,
    fund_id: int,
    txn_type: str,
    counters: FlattenCounters,
) -> TxnRow:
    # The embedded unit_fund object is duplicated fund metadata; keep only the
    # short name and drop the rest.
    embedded = txn.get("unit_fund") or txn.get("fund") or {}
    return TxnRow(
        txn_id=txn.get("id"),
        txn_type=txn_type,
        client_id=client.client_id,
        client_code=client.client_code,
        unit_fund_id=txn.get("unit_fund_id", fund_id),
        fund_short_name=embedded.get("short_name"),
        date=_parse_date(txn.get("date"), counters),
        amount=_parse_amount(txn.get("number"), counters),
        unit_price=txn.get("unit_price"),
        fees_incurred=txn.get("fees_incurred"),
        sale_type=txn.get("sale_type"),
    )


def flatten_payload(payload: dict[str, Any], reference_date: datetime) -> FlattenResult:
    """Flatten one raw payload into funds, clients, and transactions.

    reference_date anchors days since last activity. It is required, with no
    wall-clock fallback, so the same payload and anchor always flatten the same
    way. Its calendar date is used, taken in whatever zone the datetime carries;
    flatten_run supplies the run's persisted EAT timestamp.
    """
    ref = reference_date.date()
    result = FlattenResult()
    seen_funds: set[int] = set()

    env = RawEnvelope.model_validate(payload)
    for fund_raw in env.data:
        try:
            fund = FundRecord.model_validate(fund_raw)
        except ValidationError:
            result.counters.funds_skipped += 1
            continue

        if fund.unit_fund_id not in seen_funds:
            seen_funds.add(fund.unit_fund_id)
            result.funds.append(
                FundRow(fund.unit_fund_id, fund.unit_fund_name, fund.inactive_client_count)
            )

        for client_raw in fund.clients:
            try:
                client = ClientRecord.model_validate(client_raw)
            except ValidationError:
                result.counters.clients_skipped += 1
                continue

            purchases = client_raw.get("last_5_purchases") or []
            sales = client_raw.get("last_2_sales") or []
            purchase_rows = [
                _txn_row(t, client, fund.unit_fund_id, "purchase", result.counters)
                for t in purchases
            ]
            sale_rows = [
                _txn_row(t, client, fund.unit_fund_id, "sale", result.counters) for t in sales
            ]
            result.transactions.extend(purchase_rows)
            result.transactions.extend(sale_rows)

            last_purchase = _max_date([r.date for r in purchase_rows])
            last_sale = _max_date([r.date for r in sale_rows])
            last_activity = _max_date([last_purchase, last_sale])
            total_purchase = sum(r.amount for r in purchase_rows)
            total_sale = sum(r.amount for r in sale_rows)

            # A full purchase window hides older purchases. A full window of
            # either kind means the client's real history is truncated.
            purchases_censored = len(purchases) >= PURCHASE_CAP
            history_censored = purchases_censored or len(sales) >= SALE_CAP

            result.clients.append(
                ClientRow(
                    client_id=client.client_id,
                    client_code=client.client_code,
                    client_name=client.client_name,
                    unit_fund_id=fund.unit_fund_id,
                    balance=client.balance,
                    n_purchases_returned=len(purchases),
                    n_sales_returned=len(sales),
                    last_purchase_date=last_purchase,
                    last_sale_date=last_sale,
                    total_purchase_amount=total_purchase,
                    total_sale_amount=total_sale,
                    last_activity_date=last_activity,
                    days_since_last_activity=(ref - last_activity).days if last_activity else None,
                    computed_at=client.computed_at,
                    purchases_censored=purchases_censored,
                    history_censored=history_censored,
                )
            )

    return result


def _load_reference_ts(session: Session, run_id: str) -> datetime:
    """Return the run's persisted anchor timestamp for recency math.

    Raises when the run or its reference timestamp is missing, since without the
    anchor the derivations would not be reproducible.
    """
    ref = session.execute(
        select(IngestionStatus.reference_ts).where(IngestionStatus.run_id == run_id)
    ).scalar_one_or_none()
    if ref is None:
        raise ValueError(f"run {run_id} has no persisted reference_ts to anchor days_since_*")
    return ref


def flatten_run(
    session: Session, run_id: str, reference_date: datetime | None = None
) -> FlattenResult:
    """Read a run's raw staging pages and flatten them into one result.

    Funds are de-duplicated across pages. The anchor for days_since_* is the run's
    persisted reference_ts, read back here, so re-running the same run reproduces
    identical derivations. Pass reference_date only to override that anchor.
    """
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

    combined = FlattenResult()
    seen_funds: set[int] = set()
    for payload in payloads:
        page = flatten_payload(payload, reference_date=ref)
        for fund in page.funds:
            if fund.unit_fund_id not in seen_funds:
                seen_funds.add(fund.unit_fund_id)
                combined.funds.append(fund)
        combined.clients.extend(page.clients)
        combined.transactions.extend(page.transactions)
        combined.counters.merge(page.counters)

    return combined
