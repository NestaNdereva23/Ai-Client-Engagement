"""Models for the inactive clients response."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

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
    "client_email",
    "client_phone",
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
    model_config = ConfigDict(extra="ignore")

    client_id: int
    client_code: int | str | None = None
    client_name: str | None = None
    client_email: str | None = None
    client_phone: str | None = None
    balance: float | None = 0
    computed_at: str | None = None
    last_5_purchases: list[TransactionRecord] = Field(default_factory=list)
    last_2_sales: list[TransactionRecord] = Field(default_factory=list)


class FundRecord(BaseModel):
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
