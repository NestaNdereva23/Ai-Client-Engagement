"""Maps an ingestion endpoint label to the pieces IngestionWorker needs.

One place that knows both feeds exist, so a caller only has to name the
endpoint ("inactive-clients" or "active-clients") and gets back the right
fetch path, contract models, and reconciliation field, instead of every
caller repeating that mapping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.ingestion.contracts import ClientRecord, FundRecord, schema_drift
from app.ingestion.contracts_active import (
    ActiveClientRecord,
    ActiveFundRecord,
    schema_drift_active,
)


@dataclass(frozen=True)
class EndpointConfig:
    """Everything IngestionWorker needs to run one endpoint, besides the run id."""

    fetch_path: str | None
    fund_model: type
    client_model: type
    schema_drift_fn: Callable[[dict[str, Any]], set[str]]
    count_field: str


class UnknownEndpoint(ValueError):
    """Raised when an endpoint label has no known mapping."""


def resolve_endpoint(endpoint: str, settings: Settings) -> EndpointConfig:
    """Return the worker configuration for one endpoint label.

    fetch_path is None for "inactive-clients", which is how IngestionWorker
    already falls back to the client's own base URL; every other endpoint
    supplies its own full URL from settings.
    """
    if endpoint == "inactive-clients":
        return EndpointConfig(
            fetch_path=None,
            fund_model=FundRecord,
            client_model=ClientRecord,
            schema_drift_fn=schema_drift,
            count_field="inactive_client_count",
        )
    if endpoint == "active-clients":
        return EndpointConfig(
            fetch_path=settings.cytonn_active_clients_url,
            fund_model=ActiveFundRecord,
            client_model=ActiveClientRecord,
            schema_drift_fn=schema_drift_active,
            count_field="client_count",
        )
    raise UnknownEndpoint(endpoint)
