from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.campaigns.enrollment import resolve_primary_client_ids
from app.db.models.models import Clients
from app.services.clients import resolve_cohort_client_ids


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
    valued_count, estimated_value = session.execute(
        select(
            func.count(Clients.client_id),
            func.coalesce(func.sum(Clients.total_purchase_amount), 0.0),
        ).where(Clients.client_id.in_(primary_ids))
    ).one()

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
    narrow_client_ids = resolve_cohort_client_ids(session, **narrow_filters)
    narrow_preview = _preview_from_client_ids(session, narrow_client_ids)
    narrow = NarrowPreview(
        matched_count=narrow_preview.matched_count,
        estimated_value=narrow_preview.estimated_value,
    )

    angle_previews = []
    for angle in angles:
        angle_preview = preview_cohort(session, {**narrow_filters, "message_angle": angle})
        angle_previews.append(
            AnglePreview(
                message_angle=angle,
                matched_count=angle_preview.matched_count,
                estimated_value=angle_preview.estimated_value,
            )
        )

    return BatchCohortPreview(narrow=narrow, angles=angle_previews)
