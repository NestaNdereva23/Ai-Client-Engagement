"""Flatten the active-clients payload into funds, clients, and transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.models import IngestionStatus, RawStaging
from app.ingestion.contracts_active import ActiveClientRecord, ActiveFundRecord
from app.transform.flatten import PURCHASE_CAP as DEPOSIT_CAP
from app.transform.flatten import SALE_CAP as WITHDRAWAL_CAP
from app.transform.flatten import FlattenCounters, max_date, parse_amount, parse_date


@dataclass
class ActiveClientRow:
    client_id: int
    client_code: int | str | None
    client_name: str | None
    client_email: str | None
    client_phone: str | None
    unit_fund_id: int
    balance: float | None
    n_deposits: int
    n_withdrawals: int
    last_deposit_date: date | None
    last_withdrawal_slot_date: date | None
    deposit_count_capped: bool
    withdrawal_history_hidden: bool
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

            deposits = client_raw.get("last_5_purchases") or []
            withdrawals = client_raw.get("last_2_sales") or []
            deposit_rows = [
                _active_txn_row(t, client, fund.unit_fund_id, "purchase", result.counters)
                for t in deposits
            ]
            withdrawal_rows = [
                _active_txn_row(t, client, fund.unit_fund_id, "sale", result.counters)
                for t in withdrawals
            ]
            result.transactions.extend(deposit_rows)
            result.transactions.extend(withdrawal_rows)

            last_deposit_date = max_date([r.date for r in deposit_rows])
            last_withdrawal_slot_date = max_date([r.date for r in withdrawal_rows])

            deposit_count_capped = len(deposits) >= DEPOSIT_CAP
            withdrawal_history_hidden = len(withdrawals) >= WITHDRAWAL_CAP

            result.clients.append(
                ActiveClientRow(
                    client_id=client.client_id,
                    client_code=client.client_code,
                    client_name=client.client_name,
                    client_email=client.client_email,
                    client_phone=client.client_phone,
                    unit_fund_id=fund.unit_fund_id,
                    balance=client.balance,
                    n_deposits=len(deposits),
                    n_withdrawals=len(withdrawals),
                    last_deposit_date=last_deposit_date,
                    last_withdrawal_slot_date=last_withdrawal_slot_date,
                    deposit_count_capped=deposit_count_capped,
                    withdrawal_history_hidden=withdrawal_history_hidden,
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
