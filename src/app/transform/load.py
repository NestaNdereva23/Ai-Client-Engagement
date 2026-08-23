from __future__ import annotations

from collections import Counter
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
from app.transform.features import (
    VALUE_BAND_CUTOFFS,
    FeatureRow,
    RelationshipMeasures,
    derive_features,
    derive_relationship_measures,
    largest_first,
    relationships_by_client,
)
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
    "avg_ticket",
    "max_ticket",
    "rhythm_days",
    "first_purchase",
    "active_window_days",
    "ticket_trend",
    "first_sale",
    "drawdown_days",
    "hold_days",
    "exit_type",
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
_FEATURE_UPDATE = [
    "own_rhythm_days",
    "observed_volume",
    "purchases_censored",
    "history_censored",
    "n_funds",
    "recency_band",
    "value_band",
    "cadence_band",
    "hold_band",
    "purchase_depth",
    "trend_band",
    "exit_reason",
    "fund_type",
    "in_wave",
    "has_depth",
    "staged_exit",
    "stale_contact",
    "newly_dormant",
    "holds_other_funds",
    "priority_tier",
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


def _client_fund_dict(c: ClientRow, m: RelationshipMeasures, *, is_primary: bool) -> dict[str, Any]:
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
        "avg_ticket": m.avg_ticket,
        "max_ticket": m.max_ticket,
        "rhythm_days": m.rhythm_days,
        "first_purchase": m.first_purchase,
        "active_window_days": m.active_window_days,
        "ticket_trend": m.ticket_trend,
        "first_sale": m.first_sale,
        "drawdown_days": m.drawdown_days,
        "hold_days": m.hold_days,
        "exit_type": m.exit_type,
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


def _log_reconciliation(result: FlattenResult, by_client: dict[int, list[ClientRow]]) -> None:
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
    return {
        "client_id": c.client_id,
        "client_name": c.client_name,
        "contact_email": c.client_email,
        "contact_whatsapp": c.client_phone,
        "source": source,
    }


def _feature_dict(f: FeatureRow) -> dict[str, Any]:
    return {
        "n_funds": f.n_funds,
        "recency_band": f.recency_band,
        "value_band": f.value_band,
        "cadence_band": f.cadence_band,
        "hold_band": f.hold_band,
        "purchase_depth": f.purchase_depth,
        "trend_band": f.trend_band,
        "exit_reason": f.exit_reason,
        "fund_type": f.fund_type,
        "in_wave": f.in_wave,
        "has_depth": f.has_depth,
        "staged_exit": f.staged_exit,
        "stale_contact": f.stale_contact,
        "newly_dormant": f.newly_dormant,
        "holds_other_funds": f.holds_other_funds,
        "priority_tier": f.priority_tier,
        "client_id": f.client_id,
        "own_rhythm_days": f.own_rhythm_days,
        "observed_volume": f.observed_volume,
        "purchases_censored": f.purchases_censored,
        "history_censored": f.history_censored,
    }


# Postgres rejects a query with more than 65535 bind parameters.
_MAX_BIND_PARAMS = 65535


def upsert(
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


def upsert_vault(session: Session, rows: list[dict[str, Any]]) -> int:
    """Upsert client_name, contact_email, contact_whatsapp, and source into
    pii_vault.

    client_name and source are always overwritten with what this run saw.
    contact_email and contact_whatsapp use COALESCE instead: a client whose
    page in this run carried no email or phone keeps whatever was already on
    file, rather than a retransform blanking out a contact set by hand
    through /integration/contacts. Shared by both the dormant and active
    loaders, since they write the same vault the same way.
    """
    if not rows:
        return 0
    batch_size = max(1, _MAX_BIND_PARAMS // len(rows[0]))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        stmt = pg_insert(PiiVault).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["client_id"],
            set_={
                "client_name": stmt.excluded.client_name,
                "contact_email": func.coalesce(stmt.excluded.contact_email, PiiVault.contact_email),
                "contact_whatsapp": func.coalesce(
                    stmt.excluded.contact_whatsapp, PiiVault.contact_whatsapp
                ),
                "source": stmt.excluded.source,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)
    return len(rows)


def persist_result(
    session: Session, result: FlattenResult, source: str | None = None
) -> PersistCounts:
    by_client = relationships_by_client(result)
    measures = derive_relationship_measures(result)
    _log_reconciliation(result, by_client)

    clients: list[dict[str, Any]] = []
    client_funds: list[dict[str, Any]] = []
    vault: list[dict[str, Any]] = []
    for rows in by_client.values():
        ordered = largest_first(rows)
        primary = ordered[0]
        clients.append(_client_dict(primary, n_funds=len(ordered)))
        vault.append(_vault_dict(primary, source))
        client_funds.extend(
            _client_fund_dict(
                row, measures[(row.client_id, row.unit_fund_id)], is_primary=row is primary
            )
            for row in ordered
        )

    funds = {f.unit_fund_id: _fund_dict(f) for f in result.funds}
    txns = {t.txn_id: _txn_dict(t) for t in result.transactions if t.txn_id is not None}
    features = {f.client_id: _feature_dict(f) for f in derive_features(result, measures)}

    counts = PersistCounts()
    counts.funds = upsert(
        session,
        Funds,
        list(funds.values()),
        "unit_fund_id",
        _FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.clients = upsert(session, Clients, clients, "client_id", _CLIENT_UPDATE)
    counts.client_funds = upsert(
        session,
        ClientFund,
        client_funds,
        ("client_id", "unit_fund_id"),
        _CLIENT_FUND_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    counts.transactions = upsert(session, Transactions, list(txns.values()), "txn_id", _TXN_UPDATE)
    counts.vault = upsert_vault(session, vault)
    counts.features = upsert(
        session,
        ClientFeatures,
        list(features.values()),
        "client_id",
        _FEATURE_UPDATE,
        extra_set={"updated_at": func.now()},
    )
    logger.info(
        "transform_features_derived",
        clients=counts.clients,
        relationships=counts.client_funds,
        value_band_cutoffs=list(VALUE_BAND_CUTOFFS),
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
