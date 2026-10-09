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

from collections.abc import Iterator, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.bulk import copy_into_new_temp_table, quote_ident
from app.db.models.campaigns import Enrollment
from app.db.models.models import Clients, PiiVault
from app.db.session import restricted_session

# Postgres rejects a query with more than 65535 bind parameters. A cohort
# query has no upper bound (see resolve_cohort_client_ids), so any IN clause
# built from one must be split into batches this size or smaller.
_MAX_BIND_PARAMS = 65535


def in_batches(ids: Sequence[int]) -> Iterator[Sequence[int]]:
    """Split ids into chunks no larger than Postgres's bind parameter limit."""
    for start in range(0, len(ids), _MAX_BIND_PARAMS):
        yield ids[start : start + _MAX_BIND_PARAMS]


def _fetch_client_names(client_ids: Sequence[int]) -> dict[int, str | None]:
    """client_id to vault name for a batch of clients, read under the restricted role."""
    if not client_ids:
        return {}
    names: dict[int, str] = {}
    with restricted_session() as session:
        for batch in in_batches(client_ids):
            rows = session.execute(
                select(PiiVault.client_id, PiiVault.client_name).where(
                    PiiVault.client_id.in_(batch)
                )
            ).all()
            names.update({row.client_id: row.client_name for row in rows})
        record_audit(
            session,
            entity_type="pii_vault",
            action="read_batch",
            detail={"count": len(client_ids), "purpose": "enrollment_dedup"},
        )
        session.commit()
    return {client_id: names.get(client_id) or None for client_id in client_ids}


def fetch_client_names(client_ids: Sequence[int]) -> dict[int, str | None]:
    return _fetch_client_names(client_ids)


def _relationship_values(session: Session, client_ids: Sequence[int]) -> dict[int, float]:
    """Each client_id's own relationship size, sourced from its primary fund.

    Clients.total_purchase_amount already is that client_id's primary
    client_fund row's own volume, projected up by the transform.
    """
    if not client_ids:
        return {}
    values: dict[int, float] = {}
    for batch in in_batches(client_ids):
        rows = session.execute(
            select(Clients.client_id, Clients.total_purchase_amount).where(
                Clients.client_id.in_(batch)
            )
        ).all()
        values.update({row.client_id: row.total_purchase_amount or 0.0 for row in rows})
    return values


def primary_flags_from_maps(
    client_ids: Sequence[int],
    names: dict[int, str | None],
    values: dict[int, float],
    *,
    already_claimed_names: set[str],
) -> dict[int, bool]:
    flags: dict[int, bool] = {}
    claimed = set(already_claimed_names)
    ordered = sorted(client_ids, key=lambda cid: (-values.get(cid, 0.0), cid))
    for client_id in ordered:
        name = names.get(client_id)
        if not name:
            flags[client_id] = True
        elif name in claimed:
            flags[client_id] = False
        else:
            flags[client_id] = True
            claimed.add(name)
    return flags


def _rank_primary_flags(
    pool: Sequence[tuple[int, float]], *, already_claimed_names: set[str]
) -> dict[int, bool]:
    if not pool:
        return {}
    table = f"_pool_{uuid4().hex[:8]}"
    with restricted_session() as session:
        raw = session.connection().connection.driver_connection
        with raw.cursor() as cur:
            copy_into_new_temp_table(
                cur, table, {"client_id": "bigint", "value": "double precision"}, pool
            )
            cur.execute(
                "SELECT p.client_id, "
                "(row_number() OVER ("
                "PARTITION BY coalesce(pv.client_name, p.client_id::text) "
                "ORDER BY p.value DESC, p.client_id"
                ") = 1) "
                "AND (pv.client_name IS NULL OR NOT (pv.client_name = ANY(%s))) "
                "AS is_primary "
                f"FROM {quote_ident(table)} p "
                "LEFT JOIN pii_vault pv ON pv.client_id = p.client_id",
                (list(already_claimed_names),),
            )
            rows = cur.fetchall()
        record_audit(
            session,
            entity_type="pii_vault",
            action="read_batch",
            detail={"count": len(pool), "purpose": "enrollment_dedup"},
        )
        session.commit()
    return {client_id: bool(is_primary) for client_id, is_primary in rows}


def _resolve_primary_flags_for_pool(
    session: Session, client_ids: Sequence[int], *, already_claimed_names: set[str]
) -> dict[int, bool]:
    if not client_ids:
        return {}

    values = _relationship_values(session, client_ids)
    pool = [(client_id, values.get(client_id, 0.0)) for client_id in client_ids]
    return _rank_primary_flags(pool, already_claimed_names=already_claimed_names)


def _resolve_primary_flags(
    session: Session, *, campaign_id: int, new_client_ids: Sequence[int]
) -> dict[int, bool]:
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
    already_names = _fetch_client_names(already_primary_ids)
    claimed_names = {name for name in already_names.values() if name}
    return _resolve_primary_flags_for_pool(
        session, new_client_ids, already_claimed_names=claimed_names
    )


def resolve_primary_client_ids(session: Session, client_ids: Sequence[int]) -> set[int]:
    unique_ids = list(dict.fromkeys(client_ids))
    flags = _resolve_primary_flags_for_pool(session, unique_ids, already_claimed_names=set())
    return {client_id for client_id, is_primary in flags.items() if is_primary}


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

    existing: dict[int, Enrollment] = {}
    for batch in in_batches(unique_ids):
        for row in session.execute(
            select(Enrollment).where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.client_id.in_(batch),
            )
        ).scalars():
            existing[row.client_id] = row
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
