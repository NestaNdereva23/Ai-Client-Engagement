"""Models for the inactive clients response.

They follow the response shape and its quirks: amounts come as strings, dates as
mixed ISO8601 strings, client_code as an int or a string, and the history is
capped. Amounts and dates are kept as they are here; typed parsing happens in
the transform stage. A record without its required identifier is not allowed
through.

Each record is checked on its own, so one bad client can be rejected without
dropping the whole fund or page.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

# Keys we expect at each level. schema_drift uses these to spot new or renamed keys.
# The fund record's headcount field was renamed from inactive_client_count to
# client_count at source; both names stay expected so raw_staging pages pulled
# before and after the rename both read as clean.
EXPECTED_ENVELOPE_KEYS = {"data"}
EXPECTED_FUND_KEYS = {
    "unit_fund_id",
    "unit_fund_name",
    "inactive_client_count",
    "client_count",
    "clients",
}
EXPECTED_CLIENT_KEYS = {
    "client_id",
    "client_code",
    "client_name",
    "balance",
    "computed_at",
    "last_5_purchases",
    "last_2_sales",
}
EXPECTED_TXN_KEYS = {
    "id",
    "date",
    "number",
    "unit_fund_id",
    "unit_price",
    "fees_incurred",
    "unit_fund",
}


class RawEnvelope(BaseModel):
    """The top level wrapper, kept loose so funds are handled one by one."""

    model_config = ConfigDict(extra="ignore")

    data: list[dict[str, Any]] = Field(default_factory=list)


class TransactionRecord(BaseModel):
    """A single purchase or sale. Amount and date stay as strings."""

    model_config = ConfigDict(extra="ignore")

    id: int
    date: str | None = None
    number: str | None = None
    unit_fund_id: int | None = None
    unit_price: float | None = None
    fees_incurred: float | None = None

    @field_validator("number", "date", mode="before")
    @classmethod
    def _keep_as_string(cls, value: Any) -> str | None:
        """Keep amounts and dates as strings, turning stray numbers into text."""
        if value is None:
            return None
        return str(value)


class ClientRecord(BaseModel):
    """A dormant client. client_id is required; client_name is the only real
    personal data and stays only so the transform can move it to the restricted
    store."""

    model_config = ConfigDict(extra="ignore")

    client_id: int
    client_code: int | str | None = None
    client_name: str | None = None
    balance: float | None = 0
    computed_at: str | None = None
    last_5_purchases: list[TransactionRecord] = Field(default_factory=list)
    last_2_sales: list[TransactionRecord] = Field(default_factory=list)


class FundRecord(BaseModel):
    """A unit fund and its dormant clients.

    The headcount field is read under either name the source has used: the
    original inactive_client_count, or client_count after the source renamed
    it. It is a per-page count, not a fund total; summing it across pages is
    the transform stage's job, not this contract's.
    """

    model_config = ConfigDict(extra="ignore")

    unit_fund_id: int
    unit_fund_name: str | None = None
    inactive_client_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("client_count", "inactive_client_count"),
    )
    # clients stays as raw dicts so each one is checked on its own.
    clients: list[dict[str, Any]] = Field(default_factory=list)


def schema_drift(payload: dict[str, Any]) -> set[str]:
    """Return any unexpected keys found anywhere in the payload.

    An empty set means the payload matches what we expect. A non empty set means
    the shape changed, a new or renamed key, and should be looked at.
    """
    unexpected: set[str] = set(payload.keys()) - EXPECTED_ENVELOPE_KEYS
    for fund in payload.get("data", []) or []:
        if not isinstance(fund, dict):
            continue
        unexpected |= set(fund.keys()) - EXPECTED_FUND_KEYS
        for client in fund.get("clients", []) or []:
            if not isinstance(client, dict):
                continue
            unexpected |= set(client.keys()) - EXPECTED_CLIENT_KEYS
            for bucket in ("last_5_purchases", "last_2_sales"):
                for txn in client.get(bucket, []) or []:
                    if isinstance(txn, dict):
                        unexpected |= set(txn.keys()) - EXPECTED_TXN_KEYS
    return unexpected
