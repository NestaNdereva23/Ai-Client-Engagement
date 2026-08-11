"""Persist flattened active-clients output into active_client_fund.

Same idempotent-upsert pattern as transform/load.py: insert, updating the
named columns when the composite key already exists. client_name goes only
to pii_vault, the same as the dormant feed.

Per-transaction rows are not persisted here: the shared transactions table
carries a foreign key to clients, which only the dormant population lands in,
and active_client_fund's own columns only need aggregates (counts, last
dates), not the individual purchase/sale rows. flatten_active_run already
re-derives those aggregates from raw_staging on every run, so the active-book
feature derivation milestone can read transaction-level detail the same way,
straight from flatten_active_run, without a table of its own.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientFund
from app.db.models.models import IngestionStatus, PiiVault
from app.transform.active_flatten import ActiveClientRow, ActiveFlattenResult, flatten_active_run
from app.transform.load import upsert

logger = structlog.get_logger(__name__)

_ACTIVE_CLIENT_FUND_UPDATE = [
    "client_code",
    "balance",
    "n_purchases",
    "n_sales",
    "last_purchase",
    "last_sale",
    "purchases_censored",
    "redemption_history_blind",
    "computed_at",
]
_VAULT_UPDATE = ["client_name", "source"]


@dataclass
class ActivePersistCounts:
    """How many rows each table received, after de-duplication."""

    client_funds: int = 0
    vault: int = 0


def _active_client_fund_dict(c: ActiveClientRow) -> dict[str, Any]:
    return {
        "client_id": c.client_id,
        "unit_fund_id": c.unit_fund_id,
        "client_code": None if c.client_code is None else str(c.client_code),
        "balance": c.balance,
        "n_purchases": c.n_purchases,
        "n_sales": c.n_sales,
        "last_purchase": c.last_purchase,
        "last_sale": c.last_sale,
        "purchases_censored": c.purchases_censored,
        "redemption_history_blind": c.redemption_history_blind,
        "computed_at": c.computed_at,
    }


def _vault_dict(c: ActiveClientRow, source: str | None) -> dict[str, Any]:
    return {"client_id": c.client_id, "client_name": c.client_name, "source": source}


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
    """Upsert a flattened active-clients result into active_client_fund and
    the shared vault.
    """
    _log_reconciliation(result)

    client_funds = {
        (c.client_id, c.unit_fund_id): _active_client_fund_dict(c) for c in result.clients
    }
    vault = {c.client_id: _vault_dict(c, source) for c in result.clients}

    counts = ActivePersistCounts()
    counts.client_funds = upsert(
        session,
        ActiveClientFund,
        list(client_funds.values()),
        ("client_id", "unit_fund_id"),
        _ACTIVE_CLIENT_FUND_UPDATE,
        extra_set={"updated_at": func.now()},
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
