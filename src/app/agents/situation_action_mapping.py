from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models.agent import SituationActionMapping
from app.rules import versioning
from app.rules.store import MESSAGE_ANGLES, URGENCIES

OBJECTIVES = (
    "welcome",
    "educate",
    "encourage",
    "reengage",
    "follow_up",
    "protect",
    "clarify",
    "maintain_momentum",
)
CHANNELS = ("email", "sms")

_REQUIRED_FIELDS = (
    "situation",
    "action_code",
    "objective",
    "angle",
    "evidence_required",
    "channel",
)


class SituationActionMappingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MappingSpec:
    situation: str
    action_code: str
    objective: str
    angle: str
    evidence_required: str
    channel: str
    priority: str | None = None


def validate_mappings(mappings: Sequence[MappingSpec]) -> None:
    if not mappings:
        raise SituationActionMappingValidationError("a mapping version may not be empty")

    keys = [(spec.situation, spec.action_code) for spec in mappings]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise SituationActionMappingValidationError(
            f"situation/action pairs must be unique within a version: {duplicates}"
        )

    for spec in mappings:
        for field in _REQUIRED_FIELDS:
            if not str(getattr(spec, field, "")).strip():
                raise SituationActionMappingValidationError(
                    f"mapping '{spec.situation}' -> '{spec.action_code}' has an empty '{field}'"
                )
        if spec.objective not in OBJECTIVES:
            raise SituationActionMappingValidationError(
                f"mapping '{spec.situation}' has an unknown objective '{spec.objective}'"
            )
        if spec.angle not in MESSAGE_ANGLES:
            raise SituationActionMappingValidationError(
                f"mapping '{spec.situation}' has an unknown angle '{spec.angle}'"
            )
        if spec.channel not in CHANNELS:
            raise SituationActionMappingValidationError(
                f"mapping '{spec.situation}' has an unknown channel '{spec.channel}'"
            )
        if spec.priority is not None and spec.priority not in URGENCIES:
            raise SituationActionMappingValidationError(
                f"mapping '{spec.situation}' has an unknown priority '{spec.priority}'"
            )


def save_situation_action_mapping_version(
    session: Session,
    version: int,
    mappings: Sequence[MappingSpec],
    *,
    valid_from: date,
    valid_to: date | None = None,
    close_previous: bool = True,
) -> int:
    validate_mappings(mappings)

    if versioning.version_exists(session, "situation_action_mapping", version):
        raise SituationActionMappingValidationError(
            f"version {version} already exists and may not be changed"
        )

    if close_previous:
        session.execute(
            update(SituationActionMapping)
            .where(
                SituationActionMapping.version < version,
                SituationActionMapping.status == "published",
                SituationActionMapping.valid_to.is_(None),
            )
            .values(valid_to=valid_from)
        )

    published_at = datetime.now(UTC)
    session.add_all(
        SituationActionMapping(
            version=version,
            situation=spec.situation,
            action_code=spec.action_code,
            objective=spec.objective,
            angle=spec.angle,
            evidence_required=spec.evidence_required,
            channel=spec.channel,
            priority=spec.priority,
            valid_from=valid_from,
            valid_to=valid_to,
            status="published",
            published_at=published_at,
        )
        for spec in mappings
    )
    session.flush()

    if valid_to is None:
        versioning.record_published_version(
            session, "situation_action_mapping", versioning.DEFAULT_COMPONENT_KEY, version
        )

    return len(mappings)


def active_situation_action_mapping_version(session: Session, at: date) -> int | None:
    return session.scalar(
        select(SituationActionMapping.version)
        .where(
            SituationActionMapping.valid_from <= at,
            or_(SituationActionMapping.valid_to.is_(None), SituationActionMapping.valid_to > at),
        )
        .order_by(SituationActionMapping.valid_from.desc(), SituationActionMapping.version.desc())
        .limit(1)
    )


def load_active_mappings(session: Session, at: date) -> list[SituationActionMapping]:
    version = active_situation_action_mapping_version(session, at)
    if version is None:
        return []
    return list(
        session.scalars(
            select(SituationActionMapping)
            .where(SituationActionMapping.version == version)
            .order_by(SituationActionMapping.mapping_id)
        ).all()
    )


def actions_for_situation(
    session: Session, situation: str, at: date
) -> list[SituationActionMapping]:
    return [row for row in load_active_mappings(session, at) if row.situation == situation]


def action_code_for_situation(session: Session, situation: str, at: date) -> str | None:
    rows = actions_for_situation(session, situation, at)
    return rows[0].action_code if rows else None
