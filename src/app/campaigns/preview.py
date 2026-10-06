from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.campaigns.enrollment import (
    fetch_client_names,
    in_batches,
    primary_flags_from_maps,
    resolve_primary_client_ids,
)
from app.db.models.models import Clients
from app.services.clients import resolve_cohort_client_ids, resolve_cohort_members


@dataclass(frozen=True)
class CohortPreview:
    matched_count: int
    primary_count: int
    suppressed_count: int
    valued_count: int
    estimated_value: float


@dataclass(frozen=True)
class NarrowPreview:
    matched_count: int
    estimated_value: float


@dataclass(frozen=True)
class AnglePreview:
    message_angle: str
    matched_count: int
    estimated_value: float


@dataclass(frozen=True)
class BatchCohortPreview:
    narrow: NarrowPreview
    angles: list[AnglePreview]


def _preview_from_client_ids(session: Session, client_ids: Sequence[int]) -> CohortPreview:
    matched_count = len(client_ids)
    if matched_count == 0:
        return CohortPreview(
            matched_count=0,
            primary_count=0,
            suppressed_count=0,
            valued_count=0,
            estimated_value=0.0,
        )

    primary_ids = resolve_primary_client_ids(session, client_ids)
    valued_count = 0
    estimated_value = 0.0
    for batch in in_batches(list(primary_ids)):
        batch_count, batch_value = session.execute(
            select(
                func.count(Clients.client_id),
                func.coalesce(func.sum(Clients.total_purchase_amount), 0.0),
            ).where(Clients.client_id.in_(batch))
        ).one()
        valued_count += batch_count
        estimated_value += float(batch_value)

    return CohortPreview(
        matched_count=matched_count,
        primary_count=len(primary_ids),
        suppressed_count=matched_count - len(primary_ids),
        valued_count=valued_count,
        estimated_value=float(estimated_value),
    )


def preview_cohort(session: Session, cohort_filters: dict) -> CohortPreview:
    client_ids = resolve_cohort_client_ids(session, **cohort_filters)
    return _preview_from_client_ids(session, client_ids)


def preview_cohort_batch(
    session: Session, narrow_filters: dict, angles: Sequence[str]
) -> BatchCohortPreview:
    members = resolve_cohort_members(session, **narrow_filters)
    all_ids = [row.client_id for row in members]
    values = {row.client_id: float(row.total_purchase_amount or 0.0) for row in members}
    names = fetch_client_names(all_ids)

    def preview_of(client_ids: list[int]) -> tuple[int, float]:
        matched_count = len(client_ids)
        if matched_count == 0:
            return 0, 0.0
        flags = primary_flags_from_maps(client_ids, names, values, already_claimed_names=set())
        estimated_value = sum(
            values.get(cid, 0.0) for cid, is_primary in flags.items() if is_primary
        )
        return matched_count, estimated_value

    narrow_matched, narrow_value = preview_of(all_ids)
    narrow = NarrowPreview(matched_count=narrow_matched, estimated_value=narrow_value)

    ids_by_angle: dict[str, list[int]] = {angle: [] for angle in angles}
    for row in members:
        bucket = ids_by_angle.get(row.message_angle)
        if bucket is not None:
            bucket.append(row.client_id)

    angle_previews = []
    for angle in angles:
        matched_count, estimated_value = preview_of(ids_by_angle[angle])
        angle_previews.append(
            AnglePreview(
                message_angle=angle,
                matched_count=matched_count,
                estimated_value=estimated_value,
            )
        )

    return BatchCohortPreview(narrow=narrow, angles=angle_previews)
