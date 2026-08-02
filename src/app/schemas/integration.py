"""Request and response shapes for the integration-plane inbound endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator


class ContactUpsertRequest(BaseModel):
    """Contact channels and consent for one client. Either id names the client;
    at least one must be given.
    """

    client_id: int | None = None
    client_code: str | None = None
    contact_email: str | None = None
    contact_whatsapp: str | None = None
    consent: bool | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _needs_a_client_identifier(self) -> ContactUpsertRequest:
        if self.client_id is None and self.client_code is None:
            raise ValueError("either client_id or client_code is required")
        return self


class ContactUpsertOut(BaseModel):
    client_id: int
    contact_email: str | None
    contact_whatsapp: str | None
    consent: bool
    updated_at: datetime


class SuppressionRequest(BaseModel):
    client_id: int
    reason: str
    source: str | None = None


class SuppressionOut(BaseModel):
    client_id: int
    reason: str
    source: str | None
    created_at: datetime
