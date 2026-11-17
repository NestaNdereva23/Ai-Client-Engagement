"""Schema-safe versions of the catalogue load/save helpers, for data migrations.

The helpers in app code build their queries from today's ORM models, which
carry every column added up to head. A data migration that calls them runs
against the schema of its own revision, so on a fresh database (CI, a new
test database) any column a later migration adds, such as
message_angle_catalog.audience, breaks the upgrade.

These functions keep the app helpers' names and signatures but read and write
with plain SQL limited to the columns that actually exist when the migration
runs. Validation still uses the app's validators, so a migration's data is
checked exactly as before.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.rules import versioning


class Row:
    """A table row read by name; a column the table lacks yet reads as None."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return None


def _columns(session: Session, table: str) -> list[str]:
    return [c["name"] for c in sa.inspect(session.connection()).get_columns(table)]


def _quoted(names: Sequence[str]) -> str:
    return ", ".join(f'"{n}"' for n in names)


def active_version(session: Session, table: str, at: date) -> int | None:
    return session.execute(
        sa.text(
            f"SELECT version FROM {table} "
            "WHERE valid_from <= :at AND (valid_to IS NULL OR valid_to > :at) "
            "ORDER BY valid_from DESC, version DESC LIMIT 1"
        ),
        {"at": at},
    ).scalar()


def _active_rows(session: Session, table: str, order_by: str, at: date) -> list[Row]:
    version = active_version(session, table, at)
    if version is None:
        return []
    columns = _columns(session, table)
    result = session.execute(
        sa.text(f"SELECT {_quoted(columns)} FROM {table} WHERE version = :v ORDER BY {order_by}"),
        {"v": version},
    ).mappings()
    return [Row(dict(r)) for r in result]


def _write(
    session: Session,
    table: str,
    version: int,
    specs: Sequence[Any],
    *,
    valid_from: date,
    valid_to: date | None,
    close_previous: bool,
) -> None:
    exists = session.execute(
        sa.text(f"SELECT count(*) FROM {table} WHERE version = :v"), {"v": version}
    ).scalar()
    if exists:
        raise RuntimeError(f"{table} version {version} already exists and may not be changed")

    if close_previous:
        session.execute(
            sa.text(
                f"UPDATE {table} SET valid_to = :vf WHERE version < :v "
                "AND status = 'published' AND valid_to IS NULL"
            ),
            {"vf": valid_from, "v": version},
        )

    present = {c["name"]: c["type"] for c in sa.inspect(session.connection()).get_columns(table)}
    now = datetime.now(UTC)
    for spec in specs:
        values = {
            k: (list(v) if isinstance(v, tuple) else v) for k, v in dataclasses.asdict(spec).items()
        }
        values.update(
            version=version,
            valid_from=valid_from,
            valid_to=valid_to,
            status="published",
            published_at=now,
        )
        values = {k: v for k, v in values.items() if k in present}
        stmt = sa.text(
            f"INSERT INTO {table} ({_quoted(list(values))}) "
            f"VALUES ({', '.join(':' + k for k in values)})"
        ).bindparams(*(sa.bindparam(k, type_=present[k]) for k in values))
        session.execute(stmt, values)


# message_angle_catalog -------------------------------------------------------


def load_active_angles(session: Session, at: date) -> dict[str, Row]:
    return {r.angle: r for r in _active_rows(session, "message_angle_catalog", "catalog_id", at)}


def active_catalog_version(session: Session, at: date) -> int | None:
    return active_version(session, "message_angle_catalog", at)


def save_catalog_version(
    session: Session,
    version: int,
    angles: Sequence[Any],
    *,
    valid_from: date,
    valid_to: date | None = None,
) -> int:
    from app.rules.catalog import validate_angles

    validate_angles(angles)
    _write(
        session,
        "message_angle_catalog",
        version,
        angles,
        valid_from=valid_from,
        valid_to=valid_to,
        close_previous=False,
    )
    if valid_to is None:
        for spec in angles:
            versioning.record_published_version(
                session, "message_angle_catalog", spec.angle, version
            )
    return len(angles)


# agent_action_catalog --------------------------------------------------------


def load_active_actions(session: Session, at: date) -> dict[str, Row]:
    return {
        r.action_code: r for r in _active_rows(session, "agent_action_catalog", "catalog_id", at)
    }


def save_action_catalog_version(
    session: Session,
    version: int,
    actions: Sequence[Any],
    *,
    valid_from: date,
    valid_to: date | None = None,
    close_previous: bool = True,
) -> int:
    from app.agents.action_catalog import validate_actions

    validate_actions(actions)
    _write(
        session,
        "agent_action_catalog",
        version,
        actions,
        valid_from=valid_from,
        valid_to=valid_to,
        close_previous=close_previous,
    )
    if valid_to is None:
        versioning.record_published_version(
            session, "agent_action_catalog", versioning.DEFAULT_COMPONENT_KEY, version
        )
    return len(actions)


# situation_action_mapping ----------------------------------------------------


def load_active_mappings(session: Session, at: date) -> list[Row]:
    return _active_rows(session, "situation_action_mapping", "mapping_id", at)


def save_situation_action_mapping_version(
    session: Session,
    version: int,
    mappings: Sequence[Any],
    *,
    valid_from: date,
    valid_to: date | None = None,
    close_previous: bool = True,
) -> int:
    from app.agents.situation_action_mapping import validate_mappings

    validate_mappings(mappings)
    _write(
        session,
        "situation_action_mapping",
        version,
        mappings,
        valid_from=valid_from,
        valid_to=valid_to,
        close_previous=close_previous,
    )
    if valid_to is None:
        versioning.record_published_version(
            session, "situation_action_mapping", versioning.DEFAULT_COMPONENT_KEY, version
        )
    return len(mappings)
