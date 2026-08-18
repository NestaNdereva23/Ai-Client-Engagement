"""Persist flattened active-clients output into active_client_fund and
active_transaction.

Same idempotent-upsert pattern as transform/load.py: insert, updating the
named columns when the composite key already exists. client_name goes only
to pii_vault, the same as the dormant feed.

Per-transaction rows land in active_transaction, upserted on txn_id --
a separate table from the dormant feed's own transactions, since that one's
foreign key to clients only ever accepts the dormant population.
active_client_fund's own columns still only carry aggregates (counts, last
dates); the individual rows accumulate in active_transaction instead, across
every nightly run, so one that ages out of the feed's own "last 5 purchases"
/ "last 2 sales" window on a later pull stays visible rather than lost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.models import IngestionStatus, PiiVault
from app.transform.active_features import ActiveFeatureMeasures, derive_active_measures
from app.transform.active_flatten import (
    ActiveClientRow,
    ActiveFlattenResult,
    ActiveTxnRow,
    flatten_active_run,
)
from app.transform.load import upsert

logger = structlog.get_logger(__name__)

_ACTIVE_CLIENT_FUND_UPDATE = [
    "client_code",
    "balance",
    "n_deposits",
    "n_withdrawals",
    "last_deposit_date",
    "last_withdrawal_slot_date",
    "deposit_count_capped",
    "withdrawal_history_hidden",
    "computed_at",
    "typical_gap_days",
    "avg_deposit_amount",
    "max_deposit_amount",
    "last_deposit_amount",
    "deposit_trend",
    "largest_withdrawal",
    "last_withdrawal_date",
    "months_until_empty",
]
_ACTIVE_TXN_UPDATE = [
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
_VAULT_UPDATE = ["client_name", "source"]


@dataclass
class ActivePersistCounts:
    """How many rows each table received, after de-duplication."""

    client_funds: int = 0
    transactions: int = 0
    vault: int = 0


def _active_client_fund_dict(c: ActiveClientRow, measures: ActiveFeatureMeasures) -> dict[str, Any]:
    return {
        "client_id": c.client_id,
        "unit_fund_id": c.unit_fund_id,
        "client_code": None if c.client_code is None else str(c.client_code),
        "balance": c.balance,
        "n_deposits": c.n_deposits,
        "n_withdrawals": c.n_withdrawals,
        "last_deposit_date": c.last_deposit_date,
        "last_withdrawal_slot_date": c.last_withdrawal_slot_date,
        "deposit_count_capped": c.deposit_count_capped,
        "withdrawal_history_hidden": c.withdrawal_history_hidden,
        "computed_at": c.computed_at,
        "typical_gap_days": measures.typical_gap_days,
        "avg_deposit_amount": measures.avg_deposit_amount,
        "max_deposit_amount": measures.max_deposit_amount,
        "last_deposit_amount": measures.last_deposit_amount,
        "deposit_trend": measures.deposit_trend,
        "largest_withdrawal": measures.largest_withdrawal,
        "last_withdrawal_date": measures.last_withdrawal_date,
        "months_until_empty": measures.months_until_empty,
    }


def _vault_dict(c: ActiveClientRow, source: str | None) -> dict[str, Any]:
    return {"client_id": c.client_id, "client_name": c.client_name, "source": source}


def _active_txn_dict(t: ActiveTxnRow) -> dict[str, Any]:
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


def _log_reconciliation(result: ActiveFlattenResult) -> None:
    """Report per-fund headcount so a shortfall is visible, not inferred.

    The header client_count check itself runs during ingestion (workers/
    ingestion.py); this logs the same idea from the transform side, counting
    unique clients kept per fund after de-duplication across pages.
    """
    clients_by_fund = Counter(row.unit_fund_id for row in result.clients)
    if clients_by_fund:
        logger.info("active_transform_clients_by_fund", funds=dict(clients_by_fund))


def persist_active_result(
    session: Session, result: ActiveFlattenResult, source: str | None = None
) -> ActivePersistCounts:
    """Upsert a flattened active-clients result into active_client_fund,
    active_transaction, and the shared vault.
    """
    _log_reconciliation(result)

    measures = derive_active_measures(result)
    client_funds = {
        (c.client_id, c.unit_fund_id): _active_client_fund_dict(
            c, measures[(c.client_id, c.unit_fund_id)]
        )
        for c in result.clients
    }
    vault = {c.client_id: _vault_dict(c, source) for c in result.clients}
    # Keyed into a dict first, same as client_funds/vault above, so a
    # transaction repeated across pages in this run becomes one upsert.
    transactions = {
        t.txn_id: _active_txn_dict(t) for t in result.transactions if t.txn_id is not None
    }

    counts = ActivePersistCounts()
    counts.client_funds = upsert(
        session,
        ActiveClientFund,
        list(client_funds.values()),
        ("client_id", "unit_fund_id"),
        _ACTIVE_CLIENT_FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.transactions = upsert(
        session, ActiveTransaction, list(transactions.values()), "txn_id", _ACTIVE_TXN_UPDATE
    )
    counts.vault = upsert(
        session,
        PiiVault,
        list(vault.values()),
        "client_id",
        _VAULT_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    session.commit()
    return counts


def transform_active_run(session: Session, run_id: str) -> ActivePersistCounts:
    """Flatten a run's raw staging and upsert it into active_client_fund."""
    result = flatten_active_run(session, run_id)
    source = session.execute(
        select(IngestionStatus.endpoint).where(IngestionStatus.run_id == run_id)
    ).scalar_one_or_none()
    return persist_active_result(session, result, source=source)
