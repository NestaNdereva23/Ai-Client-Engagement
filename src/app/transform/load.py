"""
Persist flattened output into the normalized funds, clients, transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.models import Clients, Funds, IngestionStatus, PiiVault, Transactions
from app.transform.flatten import ClientRow, FlattenResult, FundRow, TxnRow, flatten_run

# Columns updated on conflict, one entry per table. The natural key is excluded.
_FUND_UPDATE = ["unit_fund_name", "inactive_client_count"]
_CLIENT_UPDATE = [
    "client_code",
    "unit_fund_id",
    "balance",
    "n_purchases_returned",
    "n_sales_returned",
    "last_purchase_date",
    "last_sale_date",
    "total_purchase_amount",
    "total_sale_amount",
    "last_activity_date",
    "days_since_last_activity",
    "net_flow",
    "computed_at",
]
_TXN_UPDATE = [
    "txn_type",
    "client_id",
    "unit_fund_id",
    "fund_short_name",
    "txn_date",
    "amount",
    "unit_price",
    "fees_incurred",
    "sale_type",
]
# The vault re-transform updates only the name and its source. Contact channels
# and opt-out arrive from a separate source and must survive a re-transform.
_VAULT_UPDATE = ["client_name", "source"]


@dataclass
class PersistCounts:
    """How many rows each table received, after de-duplication."""

    funds: int = 0
    clients: int = 0
    transactions: int = 0
    vault: int = 0


def _fund_dict(f: FundRow) -> dict[str, Any]:
    return {
        "unit_fund_id": f.unit_fund_id,
        "unit_fund_name": f.unit_fund_name,
        "inactive_client_count": f.inactive_client_count,
    }


def _client_dict(c: ClientRow) -> dict[str, Any]:
    return {
        "client_id": c.client_id,
        "client_code": None if c.client_code is None else str(c.client_code),
        "unit_fund_id": c.unit_fund_id,
        "balance": c.balance,
        "n_purchases_returned": c.n_purchases_returned,
        "n_sales_returned": c.n_sales_returned,
        "last_purchase_date": c.last_purchase_date,
        "last_sale_date": c.last_sale_date,
        "total_purchase_amount": c.total_purchase_amount,
        "total_sale_amount": c.total_sale_amount,
        "last_activity_date": c.last_activity_date,
        "days_since_last_activity": c.days_since_last_activity,
        "net_flow": c.net_flow,
        "computed_at": c.computed_at,
    }


def _txn_dict(t: TxnRow) -> dict[str, Any]:
    return {
        "txn_id": t.txn_id,
        "txn_type": t.txn_type,
        "client_id": t.client_id,
        "unit_fund_id": t.unit_fund_id,
        "fund_short_name": t.fund_short_name,
        "txn_date": t.date,
        "amount": t.amount,
        "unit_price": t.unit_price,
        "fees_incurred": t.fees_incurred,
        "sale_type": t.sale_type,
    }


def _vault_dict(c: ClientRow, source: str | None) -> dict[str, Any]:
    # Only the name (and its source) is written here; contact channels stay null
    # until a contact source exists.
    return {
        "client_id": c.client_id,
        "client_name": c.client_name,
        "source": source,
    }


def _upsert(
    session: Session,
    model: type,
    rows: list[dict[str, Any]],
    key: str,
    update_columns: list[str],
    extra_set: dict[str, Any] | None = None,
) -> int:
    """Insert rows, updating the named columns when the key already exists."""
    if not rows:
        return 0
    stmt = pg_insert(model).values(rows)
    set_ = {col: getattr(stmt.excluded, col) for col in update_columns}
    if extra_set:
        set_.update(extra_set)
    stmt = stmt.on_conflict_do_update(index_elements=[key], set_=set_)
    session.execute(stmt)
    return len(rows)


def persist_result(
    session: Session, result: FlattenResult, source: str | None = None
) -> PersistCounts:
    """Upsert a flattened result into funds, clients, transactions, and the vault.

    The fund/client/transaction order satisfies the foreign keys within one
    transaction. client_name is written only to the vault, never to clients. Rows
    are keyed into dicts first so a repeated natural key becomes a single upsert.
    """
    funds = {f.unit_fund_id: _fund_dict(f) for f in result.funds}
    clients = {c.client_id: _client_dict(c) for c in result.clients}
    txns = {t.txn_id: _txn_dict(t) for t in result.transactions if t.txn_id is not None}
    vault = {c.client_id: _vault_dict(c, source) for c in result.clients}

    counts = PersistCounts()
    counts.funds = _upsert(
        session,
        Funds,
        list(funds.values()),
        "unit_fund_id",
        _FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.clients = _upsert(session, Clients, list(clients.values()), "client_id", _CLIENT_UPDATE)
    counts.transactions = _upsert(session, Transactions, list(txns.values()), "txn_id", _TXN_UPDATE)
    counts.vault = _upsert(
        session,
        PiiVault,
        list(vault.values()),
        "client_id",
        _VAULT_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    session.commit()
    return counts


def transform_run(session: Session, run_id: str) -> PersistCounts:
    """Flatten a run's raw staging and upsert it into the normalized tables.

    The run's endpoint is recorded as the vault source, so the name's provenance
    travels with it.
    """
    result = flatten_run(session, run_id)
    source = session.execute(
        select(IngestionStatus.endpoint).where(IngestionStatus.run_id == run_id)
    ).scalar_one_or_none()
    return persist_result(session, result, source=source)
