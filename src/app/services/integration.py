"""Integration inbound: contact channels, suppressions, and the ingestion trigger.

The parent system pushes data ACE cannot get from the Cytonn client-data pull
itself: contact channels (unblocking R1, since that pull carries no email or
phone), and compliance suppressions. Both are client_id-keyed and restricted
(CLAUDE.md §7), so both write through restricted_session(), the same pattern
services/review.py's vault read already uses, just for a write instead. Both
are upserts, not appends: a resync overwrites only the fields it actually
carries, so a consent-only update can never blank out a previously known
email or phone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.models import Clients, PiiVault
from app.db.models.suppression import Suppression
from app.db.session import restricted_session


class ClientNotFound(Exception):
    """No clients row exists for the given client_code."""


@dataclass(frozen=True)
class ContactRecord:
    """A plain snapshot of the upserted vault row.

    Never the ORM object itself: restricted_session()'s cleanup rolls back
    on the way out to guarantee the role is reset even on an exception path,
    and a rollback unconditionally expires every attribute on every object
    still attached to that session, expire_on_commit or not. Returning the
    live object would leave the caller holding one that raises
    DetachedInstanceError on first read.
    """

    client_id: int
    contact_email: str | None
    contact_whatsapp: str | None
    consent: bool
    updated_at: datetime


@dataclass(frozen=True)
class SuppressionRecord:
    """A plain snapshot of the upserted suppression row; see ContactRecord for why."""

    client_id: int
    reason: str
    source: str | None
    created_at: datetime


def resolve_client_id(session: Session, *, client_id: int | None, client_code: str | None) -> int:
    """client_id directly, or looked up from client_code against the plain clients table."""
    if client_id is not None:
        return client_id
    if client_code is None:
        raise ValueError("either client_id or client_code is required")
    resolved = session.scalar(select(Clients.client_id).where(Clients.client_code == client_code))
    if resolved is None:
        raise ClientNotFound(client_code)
    return resolved


def upsert_contact(
    *,
    client_id: int,
    contact_email: str | None = None,
    contact_whatsapp: str | None = None,
    consent: bool | None = None,
    source: str | None = None,
) -> ContactRecord:
    """Upsert contact channels and consent for one client, under the restricted role.

    Only the fields actually given are written; an omitted field leaves
    whatever is already on file untouched rather than being cleared.
    """
    values: dict = {"client_id": client_id}
    updates: dict = {"updated_at": func.now()}
    if contact_email is not None:
        values["contact_email"] = updates["contact_email"] = contact_email
    if contact_whatsapp is not None:
        values["contact_whatsapp"] = updates["contact_whatsapp"] = contact_whatsapp
    if consent is not None:
        values["opt_out_flag"] = updates["opt_out_flag"] = not consent
    if source is not None:
        values["source"] = updates["source"] = source

    with restricted_session() as restricted:
        stmt = pg_insert(PiiVault).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=["client_id"], set_=updates)
        restricted.execute(stmt)
        record_audit(
            restricted,
            entity_type="pii_vault",
            action="upsert_contact",
            entity_id=str(client_id),
            detail={
                "contact_email_provided": contact_email is not None,
                "contact_whatsapp_provided": contact_whatsapp is not None,
                "consent": consent,
            },
        )
        restricted.commit()
        vault = restricted.get(PiiVault, client_id)
        return ContactRecord(
            client_id=vault.client_id,
            contact_email=vault.contact_email,
            contact_whatsapp=vault.contact_whatsapp,
            consent=not vault.opt_out_flag,
            updated_at=vault.updated_at,
        )


def record_suppression(
    *, client_id: int, reason: str, source: str | None = None
) -> SuppressionRecord:
    """Upsert one client onto the suppression list, under the restricted role."""
    with restricted_session() as restricted:
        stmt = pg_insert(Suppression).values(client_id=client_id, reason=reason, source=source)
        stmt = stmt.on_conflict_do_update(
            index_elements=["client_id"], set_={"reason": reason, "source": source}
        )
        restricted.execute(stmt)
        record_audit(
            restricted,
            entity_type="suppression",
            action="upsert",
            entity_id=str(client_id),
            detail={"reason": reason, "source": source},
        )
        restricted.commit()
        row = restricted.get(Suppression, client_id)
        return SuppressionRecord(
            client_id=row.client_id, reason=row.reason, source=row.source, created_at=row.created_at
        )
