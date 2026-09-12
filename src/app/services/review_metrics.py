from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.llmops import GenerationRun
from app.db.models.outreach import OutreachMessage, ReviewAction


@dataclass(frozen=True)
class AngleVersionMetrics:
    angle: str
    angle_catalog_version: int | None
    review_count: int
    approval_rate: float
    edit_rate: float
    rejection_rate: float
    regeneration_rate: float


def angle_version_metrics(session: Session, angle: str) -> list[AngleVersionMetrics]:
    rows = session.execute(
        select(GenerationRun.angle_catalog_version, ReviewAction.outcome, GenerationRun.attempts)
        .select_from(ReviewAction)
        .join(OutreachMessage, ReviewAction.message_id == OutreachMessage.message_id)
        .join(GenerationRun, OutreachMessage.generation_run_id == GenerationRun.run_id)
        .where(ReviewAction.message_angle == angle)
    ).all()

    by_version: dict[int | None, list[tuple[str, int]]] = {}
    for version, outcome, attempts in rows:
        by_version.setdefault(version, []).append((outcome, attempts))

    results: list[AngleVersionMetrics] = []
    for version in sorted(by_version, key=lambda v: (v is None, v)):
        entries = by_version[version]
        total = len(entries)
        approve = sum(1 for outcome, _ in entries if outcome == "approve")
        edit = sum(1 for outcome, _ in entries if outcome == "edit_approve")
        reject = sum(1 for outcome, _ in entries if outcome == "reject")
        regenerated = sum(1 for _, attempts in entries if attempts and attempts > 1)
        results.append(
            AngleVersionMetrics(
                angle=angle,
                angle_catalog_version=version,
                review_count=total,
                approval_rate=approve / total,
                edit_rate=edit / total,
                rejection_rate=reject / total,
                regeneration_rate=regenerated / total,
            )
        )
    return results
