from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.db.models.prompt_config import (
    ActiveConfiguration,
    OutputPolicy,
    PersonalizationPolicy,
    SafetyPolicy,
    VoiceContract,
)
from app.db.models.rules import BusinessRule, MessageAngleCatalog, TierContract

DEFAULT_COMPONENT_KEY = "default"

_BOOKKEEPING_COLUMNS = frozenset(
    {"status", "valid_from", "valid_to", "created_by", "published_by", "published_at", "created_at"}
)


class VersioningError(ValueError):
    pass


@dataclass(frozen=True)
class ComponentSpec:
    # key_column None means the whole table is one component, addressed by
    # DEFAULT_COMPONENT_KEY; a name means each value of that column (an
    # angle, a tier) is its own independently versioned component.
    model: type
    key_column: str | None = None
    version_column: str = "version"


COMPONENTS: dict[str, ComponentSpec] = {
    "message_angle_catalog": ComponentSpec(MessageAngleCatalog, key_column="angle"),
    "tier_contract": ComponentSpec(TierContract, key_column="tier"),
    "business_rules": ComponentSpec(BusinessRule),
    "voice_contract": ComponentSpec(VoiceContract),
    "safety_policy": ComponentSpec(SafetyPolicy),
    "output_policy": ComponentSpec(OutputPolicy),
    "personalization_policy": ComponentSpec(PersonalizationPolicy),
}


def _spec(component_type: str) -> ComponentSpec:
    try:
        return COMPONENTS[component_type]
    except KeyError:
        raise VersioningError(f"unknown component type '{component_type}'") from None


def _rows_for(session: Session, spec: ComponentSpec, component_key: str, version: int) -> list:
    stmt = select(spec.model).where(getattr(spec.model, spec.version_column) == version)
    if spec.key_column is not None:
        stmt = stmt.where(getattr(spec.model, spec.key_column) == component_key)
    return list(session.scalars(stmt).all())


def _live_rows(session: Session, spec: ComponentSpec, component_key: str) -> list:
    # Currently published and open-ended: the row a fresh publish must close.
    stmt = select(spec.model).where(spec.model.status == "published", spec.model.valid_to.is_(None))
    if spec.key_column is not None:
        stmt = stmt.where(getattr(spec.model, spec.key_column) == component_key)
    return list(session.scalars(stmt).all())


def _next_version(session: Session, spec: ComponentSpec, component_key: str) -> int:
    stmt = select(func.max(getattr(spec.model, spec.version_column)))
    if spec.key_column is not None:
        stmt = stmt.where(getattr(spec.model, spec.key_column) == component_key)
    current = session.scalar(stmt)
    return (current or 0) + 1


def _set_active_version(
    session: Session, component_type: str, component_key: str, version: int, when: datetime
) -> None:
    existing = session.scalar(
        select(ActiveConfiguration).where(
            ActiveConfiguration.component_type == component_type,
            ActiveConfiguration.component_key == component_key,
        )
    )
    if existing is None:
        session.add(
            ActiveConfiguration(
                component_type=component_type,
                component_key=component_key,
                active_version=version,
                updated_at=when,
            )
        )
    else:
        existing.active_version = version
        existing.updated_at = when


def _content(spec: ComponentSpec, row: object) -> dict:
    pk_columns = {column.name for column in inspect(spec.model).primary_key}
    skip = _BOOKKEEPING_COLUMNS | pk_columns | {spec.version_column}
    if spec.key_column is not None:
        skip = skip | {spec.key_column}
    return {
        column.name: getattr(row, column.name)
        for column in inspect(spec.model).columns
        if column.name not in skip
    }


def get_version_rows(
    session: Session, component_type: str, component_key: str, version: int
) -> list[dict]:
    spec = _spec(component_type)
    rows = _rows_for(session, spec, component_key, version)
    return [_content(spec, row) for row in rows]


def version_exists(session: Session, component_type: str, version: int) -> bool:
    # Table-wide, matching the check catalog.py/tier_contract.py/store.py
    # each ran on their own before this module existed.
    spec = _spec(component_type)
    column = getattr(spec.model, spec.version_column)
    return bool(session.scalar(select(func.count()).where(column == version)))


def record_published_version(
    session: Session, component_type: str, component_key: str, version: int
) -> None:
    _set_active_version(session, component_type, component_key, version, datetime.now(UTC))


def save_draft(
    session: Session,
    component_type: str,
    component_key: str,
    values: Sequence[Mapping[str, object]],
    *,
    by: str | None = None,
) -> int:
    if not values:
        raise VersioningError("a draft needs at least one row of values")

    spec = _spec(component_type)
    version = _next_version(session, spec, component_key)
    for row_values in values:
        kwargs = dict(row_values)
        kwargs[spec.version_column] = version
        if spec.key_column is not None:
            kwargs[spec.key_column] = component_key
        kwargs["status"] = "draft"
        kwargs["created_by"] = by
        kwargs.setdefault("valid_from", None)
        kwargs.setdefault("valid_to", None)
        session.add(spec.model(**kwargs))
    session.flush()
    return version


def publish(
    session: Session,
    component_type: str,
    component_key: str,
    version: int,
    *,
    by: str | None = None,
    at: date | None = None,
) -> None:
    spec = _spec(component_type)
    when = at or date.today()
    now = datetime.now(UTC)

    draft_rows = _rows_for(session, spec, component_key, version)
    if not draft_rows:
        raise VersioningError(f"no version {version} found for {component_type}:{component_key}")
    if any(row.status != "draft" for row in draft_rows):
        raise VersioningError(
            f"version {version} of {component_type}:{component_key} is not a draft"
        )

    for row in _live_rows(session, spec, component_key):
        row.valid_to = when

    for row in draft_rows:
        row.status = "published"
        row.valid_from = when
        row.valid_to = None
        row.published_by = by
        row.published_at = now

    _set_active_version(session, component_type, component_key, version, now)
    session.flush()


def discard_draft(session: Session, component_type: str, component_key: str, version: int) -> None:
    spec = _spec(component_type)
    rows = _rows_for(session, spec, component_key, version)
    if not rows:
        raise VersioningError(f"no version {version} found for {component_type}:{component_key}")
    if any(row.status != "draft" for row in rows):
        raise VersioningError(
            f"version {version} of {component_type}:{component_key} is published "
            "and may not be discarded"
        )
    for row in rows:
        session.delete(row)
    session.flush()


def list_versions(session: Session, component_type: str, component_key: str) -> list[dict]:
    spec = _spec(component_type)
    stmt = select(spec.model)
    if spec.key_column is not None:
        stmt = stmt.where(getattr(spec.model, spec.key_column) == component_key)
    rows = session.scalars(stmt).all()

    by_version: dict[int, list] = {}
    for row in rows:
        by_version.setdefault(getattr(row, spec.version_column), []).append(row)

    summaries = []
    for version in sorted(by_version):
        first = by_version[version][0]
        summaries.append(
            {
                "version": version,
                "status": first.status,
                "valid_from": first.valid_from,
                "valid_to": first.valid_to,
                "created_by": first.created_by,
                "published_by": first.published_by,
                "published_at": first.published_at,
                "row_count": len(by_version[version]),
            }
        )
    return summaries


def diff_versions(
    session: Session, component_type: str, component_key: str, version_a: int, version_b: int
) -> dict:
    # business_rules carries several rows per version (the whole prioritised
    # list), so it comes back as two row lists instead of a field diff.
    spec = _spec(component_type)
    rows_a = _rows_for(session, spec, component_key, version_a)
    rows_b = _rows_for(session, spec, component_key, version_b)
    content_a = [_content(spec, row) for row in rows_a]
    content_b = [_content(spec, row) for row in rows_b]

    if len(content_a) > 1 or len(content_b) > 1:
        return {"version_a_rows": content_a, "version_b_rows": content_b}

    a = content_a[0] if content_a else {}
    b = content_b[0] if content_b else {}
    fields = sorted(set(a) | set(b))
    return {field: (a.get(field), b.get(field)) for field in fields if a.get(field) != b.get(field)}
