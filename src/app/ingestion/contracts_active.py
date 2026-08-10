"""Models for the active clients response.

Same contract discipline as contracts.py: amounts stay as strings, dates as
mixed ISO8601 strings, client_code as an int or a string, and each record is
checked on its own so one bad client does not drop the whole fund or page.

Two things differ from the dormant feed's contract: balance here is a real,
non-zero figure rather than the dormant feed's always-zero one, and a sale
carries a sale_type field the dormant feed's contract does not model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Keys we expect at each level. schema_drift_active uses these to spot new or
# renamed keys.
EXPECTED_ACTIVE_FUND_KEYS = {
    "unit_fund_id",
    "unit_fund_name",
    "client_count",
    "clients",
}
EXPECTED_ACTIVE_CLIENT_KEYS = {
    "client_id",
    "client_code",
    "client_name",
    "balance",
    "computed_at",
    "last_5_purchases",
    "last_2_sales",
}
EXPECTED_ACTIVE_TXN_KEYS = {
    "id",
    "date",
    "number",
    "unit_fund_id",
    "unit_price",
    "fees_incurred",
    "unit_fund",
    "sale_type",
}

# Reused from contracts.py: the envelope shape is the same {"data": [...]}.
EXPECTED_ENVELOPE_KEYS = {"data"}


class ActiveTransactionRecord(BaseModel):
    """A single purchase or sale from the active feed. Amount and date stay as
    strings. sale_type is only ever populated on a sale; it is a passthrough
    field here, and only later stages decide what it means.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    date: str | None = None
    number: str | None = None
    unit_fund_id: int | None = None
    unit_price: float | None = None
    fees_incurred: float | None = None
    sale_type: str | None = None

    @field_validator("number", "date", mode="before")
    @classmethod
    def _keep_as_string(cls, value: Any) -> str | None:
        """Keep amounts and dates as strings, turning stray numbers into text."""
        if value is None:
            return None
        return str(value)


class ActiveClientRecord(BaseModel):
    """An active client. client_id is required; client_name is the only real
    personal data and stays only so the transform can move it to the
    restricted store. balance has no zero default, unlike the dormant
    contract, since an active client is expected to carry a real balance.
    """

    model_config = ConfigDict(extra="ignore")

    client_id: int
    client_code: int | str | None = None
    client_name: str | None = None
    balance: float | None = None
    computed_at: str | None = None
    last_5_purchases: list[ActiveTransactionRecord] = Field(default_factory=list)
    last_2_sales: list[ActiveTransactionRecord] = Field(default_factory=list)


class ActiveFundRecord(BaseModel):
    """A unit fund and its active clients."""

    model_config = ConfigDict(extra="ignore")

    unit_fund_id: int
    unit_fund_name: str | None = None
    client_count: int | None = None
    # clients stays as raw dicts so each one is checked on its own.
    clients: list[dict[str, Any]] = Field(default_factory=list)


def schema_drift_active(payload: dict[str, Any]) -> set[str]:
    """Return any unexpected keys found anywhere in the active-clients payload.

    An empty set means the payload matches what we expect. A non empty set
    means the shape changed, a new or renamed key, and should be looked at.
    """
    unexpected: set[str] = set(payload.keys()) - EXPECTED_ENVELOPE_KEYS
    for fund in payload.get("data", []) or []:
        if not isinstance(fund, dict):
            continue
        unexpected |= set(fund.keys()) - EXPECTED_ACTIVE_FUND_KEYS
        for client in fund.get("clients", []) or []:
            if not isinstance(client, dict):
                continue
            unexpected |= set(client.keys()) - EXPECTED_ACTIVE_CLIENT_KEYS
            for bucket in ("last_5_purchases", "last_2_sales"):
                for txn in client.get(bucket, []) or []:
                    if isinstance(txn, dict):
                        unexpected |= set(txn.keys()) - EXPECTED_ACTIVE_TXN_KEYS
    return unexpected
