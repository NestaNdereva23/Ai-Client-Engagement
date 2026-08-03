"""Persist flattened output into the normalized funds, clients, transactions.

Flatten produces one row per client and fund. Those land in client_fund, and
each client is then projected to a single clients row taken from their largest
relationship, because a person receives one message however many funds they
held. Which relationship that is gets recorded rather than left to chance.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.models import (
    ClientFeatures,
    ClientFund,
    Clients,
    Funds,
    IngestionStatus,
    PiiVault,
    Transactions,
)
from app.transform.features import FeatureRow, derive_features
from app.transform.flatten import ClientRow, FlattenResult, FundRow, TxnRow, flatten_run

logger = structlog.get_logger(__name__)

# Columns updated on conflict, one entry per table.
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
    "computed_at",
    "purchases_censored",
    "n_funds",
]
_CLIENT_FUND_UPDATE = [
    "client_code",
    "balance",
    "n_purchases",
    "n_sales",
    "last_purchase",
    "last_sale",
    "exit_date",
    "days_cold",
    "observed_volume",
    "purchases_censored",
    "history_censored",
    "is_primary_contact_row",
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
# The vault retransform updates only the name and its source. Contact channels
# and opt-out arrive from a separate source
_VAULT_UPDATE = ["client_name", "source"]
_FEATURE_UPDATE = [
    "archetype",
    "recency_bucket",
    "value_tier",
    "own_rhythm_days",
    "rhythm_band",
    "observed_volume",
    "purchases_censored",
    "history_censored",
]


@dataclass
class PersistCounts:
    """How many rows each table received, after de-duplication."""

    funds: int = 0
    clients: int = 0
    client_funds: int = 0
    transactions: int = 0
    vault: int = 0
    features: int = 0


def _fund_dict(f: FundRow) -> dict[str, Any]:
    return {
        "unit_fund_id": f.unit_fund_id,
        "unit_fund_name": f.unit_fund_name,
        "inactive_client_count": f.inactive_client_count,
    }


def _client_dict(c: ClientRow, n_funds: int) -> dict[str, Any]:
    """The person-level row, taken from the client's largest relationship."""
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
        "computed_at": c.computed_at,
        "purchases_censored": c.purchases_censored,
        "n_funds": n_funds,
    }


def _client_fund_dict(c: ClientRow, *, is_primary: bool) -> dict[str, Any]:
    return {
        "client_id": c.client_id,
        "unit_fund_id": c.unit_fund_id,
        "client_code": None if c.client_code is None else str(c.client_code),
        "balance": c.balance,
        "n_purchases": c.n_purchases_returned,
        "n_sales": c.n_sales_returned,
        "last_purchase": c.last_purchase_date,
        "last_sale": c.last_sale_date,
        "exit_date": c.last_activity_date,
        "days_cold": c.days_since_last_activity,
        "observed_volume": c.total_purchase_amount,
        "purchases_censored": c.purchases_censored,
        "history_censored": c.history_censored,
        "is_primary_contact_row": is_primary,
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


def _largest_first(rows: list[ClientRow]) -> list[ClientRow]:
    """One client's relationships, largest observed purchase volume first.

    The lowest fund id breaks a tie, so the same input always picks the same
    relationship to contact on.
    """
    return sorted(rows, key=lambda r: (-r.total_purchase_amount, r.unit_fund_id))


def _relationships(result: FlattenResult) -> dict[int, list[ClientRow]]:
    """Group the flattened rows by client, one entry per fund they hold.

    A pair repeated across pages keeps its last occurrence, matching what the
    upsert would leave behind.
    """
    unique: dict[tuple[int, int], ClientRow] = {
        (row.client_id, row.unit_fund_id): row for row in result.clients
    }
    by_client: dict[int, list[ClientRow]] = defaultdict(list)
    for row in unique.values():
        by_client[row.client_id].append(row)
    return by_client


def _log_reconciliation(result: FlattenResult, by_client: dict[int, list[ClientRow]]) -> None:
    """Report how the rows we kept compare with what the source described.

    Neither gap is an error. Repeated pages are expected on a resumed pull, and
    a fund can report a client count it does not return rows for; both are worth
    counting so the shortfall is visible rather than inferred.
    """
    kept = sum(len(rows) for rows in by_client.values())
    repeated = len(result.clients) - kept
    if repeated:
        logger.info("transform_repeated_relationships", collapsed=repeated, kept=kept)

    per_fund = Counter(row.unit_fund_id for rows in by_client.values() for row in rows)
    for fund in result.funds:
        returned = per_fund.get(fund.unit_fund_id, 0)
        if fund.inactive_client_count is not None and fund.inactive_client_count != returned:
            logger.warning(
                "transform_fund_count_mismatch",
                unit_fund_id=fund.unit_fund_id,
                reported=fund.inactive_client_count,
                returned=returned,
            )


def _vault_dict(c: ClientRow, source: str | None) -> dict[str, Any]:
    # Only the name (and its source) is written here; contact channels stay null
    # until a contact source exists.
    return {
        "client_id": c.client_id,
        "client_name": c.client_name,
        "source": source,
    }


def _feature_dict(f: FeatureRow) -> dict[str, Any]:
    return {
        "client_id": f.client_id,
        "archetype": f.archetype,
        "recency_bucket": f.recency_bucket,
        "value_tier": f.value_tier,
        "own_rhythm_days": f.own_rhythm_days,
        "rhythm_band": f.rhythm_band,
        "observed_volume": f.observed_volume,
        "purchases_censored": f.purchases_censored,
        "history_censored": f.history_censored,
    }


# Postgres rejects a query with more than 65535 bind parameters. A single
# multi-row INSERT ... VALUES (...), (...) binds row_count * column_count of
# them, so a wide table (Clients, 15 columns) hits the cap at a few thousand
# rows -- well within one ingestion run's size. Batch to stay under it.
_MAX_BIND_PARAMS = 65535


def _upsert(
    session: Session,
    model: type,
    rows: list[dict[str, Any]],
    key: str | Sequence[str],
    update_columns: list[str],
    extra_set: dict[str, Any] | None = None,
) -> int:
    """Insert rows, updating the named columns when the key already exists.

    key is one column name or several for a composite key. Batched so a large
    run never builds a single INSERT past Postgres's bind-parameter limit,
    however many columns the row carries.
    """
    if not rows:
        return 0

    index_elements = [key] if isinstance(key, str) else list(key)
    batch_size = max(1, _MAX_BIND_PARAMS // len(rows[0]))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        stmt = pg_insert(model).values(batch)
        set_ = {col: getattr(stmt.excluded, col) for col in update_columns}
        if extra_set:
            set_.update(extra_set)
        stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=set_)
        session.execute(stmt)
    return len(rows)


def persist_result(
    session: Session, result: FlattenResult, source: str | None = None
) -> PersistCounts:
    """Upsert a flattened result into funds, clients, transactions, vault, features.

    Clients land before their relationships, transactions and features so the
    foreign keys hold within one transaction. client_name is written only to the
    vault, never to clients. Rows are keyed into dicts first so a repeated
    natural key becomes a single upsert.
    """
    by_client = _relationships(result)
    _log_reconciliation(result, by_client)

    clients: list[dict[str, Any]] = []
    client_funds: list[dict[str, Any]] = []
    vault: list[dict[str, Any]] = []
    for rows in by_client.values():
        ordered = _largest_first(rows)
        primary = ordered[0]
        clients.append(_client_dict(primary, n_funds=len(ordered)))
        vault.append(_vault_dict(primary, source))
        client_funds.extend(_client_fund_dict(row, is_primary=row is primary) for row in ordered)

    funds = {f.unit_fund_id: _fund_dict(f) for f in result.funds}
    txns = {t.txn_id: _txn_dict(t) for t in result.transactions if t.txn_id is not None}
    features = {f.client_id: _feature_dict(f) for f in derive_features(result)}

    counts = PersistCounts()
    counts.funds = _upsert(
        session,
        Funds,
        list(funds.values()),
        "unit_fund_id",
        _FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.clients = _upsert(session, Clients, clients, "client_id", _CLIENT_UPDATE)
    counts.client_funds = _upsert(
        session,
        ClientFund,
        client_funds,
        ("client_id", "unit_fund_id"),
        _CLIENT_FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.transactions = _upsert(session, Transactions, list(txns.values()), "txn_id", _TXN_UPDATE)
    counts.vault = _upsert(
        session,
        PiiVault,
        vault,
        "client_id",
        _VAULT_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.features = _upsert(
        session,
        ClientFeatures,
        list(features.values()),
        "client_id",
        _FEATURE_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    session.commit()
    return counts


def transform_run(session: Session, run_id: str) -> PersistCounts:
    """Flatten a run's raw staging and upsert it into the normalized tables."""
    result = flatten_run(session, run_id)
    source = session.execute(
        select(IngestionStatus.endpoint).where(IngestionStatus.run_id == run_id)
    ).scalar_one_or_none()
    return persist_result(session, result, source=source)
