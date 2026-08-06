"""Read and write the versioned angle catalogue, with validation.

A catalogue ships as a numbered version with a validity window and is never
mutated afterwards. Editing means saving a new version, so a message sent last
month can still be read against the brief that produced it.

Selection mirrors the rule store: the active version is the one with the latest
valid_from that has started and not ended, so a catalogue and a rule set moved
together stay in step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.rules import MessageAngleCatalog

# Every field carries meaning a message depends on, so none may be blank.
_REQUIRED_FIELDS = ("angle", "headline", "who", "claim", "ask", "never")


class CatalogValidationError(ValueError):
    """A catalogue failed validation and was not written."""


@dataclass(frozen=True)
class AngleSpec:
    """One angle's brief: who it addresses, what it may say, what it may not.

    held stops a client's message from sending while everything upstream of
    that, resolution, generation, review, still runs normally.
    """

    angle: str
    headline: str
    who: str
    claim: str
    ask: str
    never: str
    held: bool = False


def validate_angles(angles: Sequence[AngleSpec]) -> None:
    """Raise CatalogValidationError unless the catalogue is well formed."""
    if not angles:
        raise CatalogValidationError("a catalogue may not be empty")

    identifiers = [a.angle for a in angles]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        raise CatalogValidationError(f"angle identifiers must be unique: {duplicates}")

    for spec in angles:
        for field in _REQUIRED_FIELDS:
            if not str(getattr(spec, field, "")).strip():
                raise CatalogValidationError(f"angle '{spec.angle}' has an empty '{field}'")


def save_catalog_version(
    session: Session,
    version: int,
    angles: Sequence[AngleSpec],
    *,
    valid_from: date,
    valid_to: date | None = None,
) -> int:
    """Validate and insert a new catalogue version, returning the row count.

    Refuses to touch a version that already exists, so a shipped catalogue is
    never edited underneath a message that already cited it.
    """
    validate_angles(angles)

    if session.scalar(select(func.count()).where(MessageAngleCatalog.version == version)):
        raise CatalogValidationError(f"version {version} already exists and may not be mutated")

    session.add_all(
        MessageAngleCatalog(
            version=version,
            angle=spec.angle,
            headline=spec.headline,
            who=spec.who,
            claim=spec.claim,
            ask=spec.ask,
            never=spec.never,
            held=spec.held,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for spec in angles
    )
    session.flush()
    return len(angles)


def active_catalog_version(session: Session, at: date) -> int | None:
    """The catalogue version in force on `at`, or None if there is none."""
    return session.scalar(
        select(MessageAngleCatalog.version)
        .where(
            MessageAngleCatalog.valid_from <= at,
            or_(MessageAngleCatalog.valid_to.is_(None), MessageAngleCatalog.valid_to > at),
        )
        .order_by(MessageAngleCatalog.valid_from.desc(), MessageAngleCatalog.version.desc())
        .limit(1)
    )


def load_active_angles(session: Session, at: date) -> dict[str, MessageAngleCatalog]:
    """The active catalogue for `at`, keyed by angle identifier."""
    version = active_catalog_version(session, at)
    if version is None:
        return {}
    rows = session.scalars(
        select(MessageAngleCatalog)
        .where(MessageAngleCatalog.version == version)
        .order_by(MessageAngleCatalog.catalog_id)
    ).all()
    return {row.angle: row for row in rows}


def load_angle(session: Session, angle: str, at: date) -> MessageAngleCatalog | None:
    """One angle's brief from the catalogue in force on `at`."""
    return load_active_angles(session, at).get(angle)


def angle_is_held(session: Session, angle: str, at: date) -> bool:
    """Whether this angle's messages are currently held from sending.

    An angle with no catalogue entry (an older rule set's angle, or one not
    yet catalogued) is never held; only an explicit row can hold one.
    """
    row = load_angle(session, angle, at)
    return bool(row is not None and row.held)
