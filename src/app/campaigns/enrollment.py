"""Enrolling a cohort of clients into a campaign, one row per client_id.

A real person can show up under more than one client_id: the source does not
guarantee a stable id across registrations, so the same client sometimes gets
a new client_id and client_code each time. Enrolling and sending from every
one of those rows would mean that person gets a separate email per
registration. is_primary_contact_row on Enrollment marks exactly one row per
group of same-named clients as the one allowed to actually generate and send
a touch, the one with the largest relationship; the rest stay enrolled, for
record keeping and so the same idempotency rules apply to them, but never
send.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.campaigns import Enrollment
from app.db.models.models import Clients, PiiVault
from app.db.session import restricted_session


def _fetch_client_names(client_ids: Sequence[int]) -> dict[int, str | None]:
    """client_id to vault name for a batch of clients, read under the restricted role."""
    if not client_ids:
        return {}
    with restricted_session() as session:
        rows = session.execute(
            select(PiiVault.client_id, PiiVault.client_name).where(
                PiiVault.client_id.in_(client_ids)
            )
        ).all()
        record_audit(
            session,
            entity_type="pii_vault",
            action="read_batch",
            detail={"count": len(client_ids), "purpose": "enrollment_dedup"},
        )
        session.commit()
    names = {row.client_id: row.client_name for row in rows}
    return {client_id: names.get(client_id) or None for client_id in client_ids}


def _relationship_values(session: Session, client_ids: Sequence[int]) -> dict[int, float]:
    """Each client_id's own relationship size, sourced from its primary fund.

    Clients.total_purchase_amount already is that client_id's primary
    client_fund row's own volume, projected up by the transform.
    """
    if not client_ids:
        return {}
    rows = session.execute(
        select(Clients.client_id, Clients.total_purchase_amount).where(
            Clients.client_id.in_(client_ids)
        )
    ).all()
    return {row.client_id: row.total_purchase_amount or 0.0 for row in rows}


def _resolve_primary_flags(
    session: Session, *, campaign_id: int, new_client_ids: Sequence[int]
) -> dict[int, bool]:
    """Decide which of new_client_ids should be the primary row for its person.

    A shared vault name usually means one real person registered more than
    once (Cytonn's client_id is not stable across every registration), not a
    coincidence, so this still dedupes by name. A person who already has a
    primary row in this campaign keeps it. Among new client_ids that share an
    unclaimed name, the one with the larger relationship wins, the same
    largest-relationship-wins principle client_fund applies within one
    client_id; a tied or missing value falls back to the lower client_id so
    the outcome stays deterministic.
    """
    if not new_client_ids:
        return {}

    already_primary_ids = list(
        session.execute(
            select(Enrollment.client_id).where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.is_primary_contact_row.is_(True),
            )
        ).scalars()
    )

    names = _fetch_client_names([*already_primary_ids, *new_client_ids])
    claimed_names = {names[cid] for cid in already_primary_ids if names.get(cid)}
    values = _relationship_values(session, new_client_ids)

    flags: dict[int, bool] = {}
    claimed_this_call: set[str] = set()
    ordered = sorted(new_client_ids, key=lambda cid: (-values.get(cid, 0.0), cid))
    for client_id in ordered:
        name = names.get(client_id)
        if not name:
            flags[client_id] = True
        elif name in claimed_names or name in claimed_this_call:
            flags[client_id] = False
        else:
            flags[client_id] = True
            claimed_this_call.add(name)
    return flags


def enroll_cohort(
    session: Session, *, campaign_id: int, client_ids: Sequence[int]
) -> list[Enrollment]:
    """Enroll a cohort of client_ids into a campaign, deduped to one primary row per person.

    A client_id already enrolled in this campaign is left exactly as it is,
    never re-inserted and never re-flagged, so calling this again with an
    overlapping cohort creates no duplicates. Returns every enrollment row
    for the given client_ids, old and new, in no particular order.
    """
    unique_ids = list(dict.fromkeys(client_ids))
    if not unique_ids:
        return []

    existing = {
        row.client_id: row
        for row in session.execute(
            select(Enrollment).where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.client_id.in_(unique_ids),
            )
        ).scalars()
    }
    new_ids = [client_id for client_id in unique_ids if client_id not in existing]
    primary_flags = _resolve_primary_flags(session, campaign_id=campaign_id, new_client_ids=new_ids)

    created: list[Enrollment] = []
    for client_id in new_ids:
        row = Enrollment(
            campaign_id=campaign_id,
            client_id=client_id,
            is_primary_contact_row=primary_flags.get(client_id, True),
        )
        session.add(row)
        created.append(row)

    if created:
        session.flush()
        record_audit(
            session,
            entity_type="enrollment",
            action="enroll_cohort",
            entity_id=str(campaign_id),
            detail={
                "enrolled_client_ids": new_ids,
                "primary_client_ids": [cid for cid in new_ids if primary_flags.get(cid)],
            },
        )

    return [*existing.values(), *created]
