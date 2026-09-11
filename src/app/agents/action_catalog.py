"""Read and write the versioned list of actions the agent may take.

A catalogue ships as a numbered version with a validity window and is never
changed afterwards. Editing means saving a new version and closing the old
one, so a proposal made months ago can still be read against the wording that
produced it.

Selection matches the angle catalogue: the version in force is the one with
the latest valid_from that has started and not ended.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.db.models.agent import (
    CONTENT_MIXES,
    PERMISSION_LEVELS,
    RESPONSE_KINDS,
    AgentActionCatalog,
)

_REQUIRED_FIELDS = ("action_code", "title", "who", "evidence_required")


class ActionCatalogValidationError(ValueError):
    """A catalogue failed validation and was not written."""


@dataclass(frozen=True)
class ActionSpec:
    """One action: who it is for, what it needs, and how far it may go alone.

    message_angle and channel are empty only for an action that sends nothing.
    money_ceiling_kes is empty when the action moves no money. paused stops
    this one action without touching the rest of the version.
    """

    action_code: str
    title: str
    who: str
    evidence_required: str
    response_kind: str
    content_mix: str
    default_permission: str
    message_angle: str | None = None
    channel: str | None = None
    money_ceiling_kes: float | None = None
    paused: bool = False


def validate_actions(actions: Sequence[ActionSpec]) -> None:
    """Raise ActionCatalogValidationError unless the catalogue is well formed."""
    if not actions:
        raise ActionCatalogValidationError("a catalogue may not be empty")

    codes = [a.action_code for a in actions]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise ActionCatalogValidationError(f"action codes must be unique: {duplicates}")

    for spec in actions:
        for field in _REQUIRED_FIELDS:
            if not str(getattr(spec, field, "")).strip():
                raise ActionCatalogValidationError(
                    f"action '{spec.action_code}' has an empty '{field}'"
                )
        if spec.response_kind not in RESPONSE_KINDS:
            raise ActionCatalogValidationError(
                f"action '{spec.action_code}' has an unknown response kind '{spec.response_kind}'"
            )
        if spec.content_mix not in CONTENT_MIXES:
            raise ActionCatalogValidationError(
                f"action '{spec.action_code}' has an unknown content mix '{spec.content_mix}'"
            )
        if spec.default_permission not in PERMISSION_LEVELS:
            raise ActionCatalogValidationError(
                f"action '{spec.action_code}' has an unknown permission level "
                f"'{spec.default_permission}'"
            )
        if spec.money_ceiling_kes is not None and spec.money_ceiling_kes <= 0:
            raise ActionCatalogValidationError(
                f"action '{spec.action_code}' has a money ceiling that is not above zero"
            )
        sends_a_message = bool(spec.message_angle) or bool(spec.channel)
        if sends_a_message and not (spec.message_angle and spec.channel):
            raise ActionCatalogValidationError(
                f"action '{spec.action_code}' must give both a message angle and a "
                "channel, or neither"
            )


def save_action_catalog_version(
    session: Session,
    version: int,
    actions: Sequence[ActionSpec],
    *,
    valid_from: date,
    valid_to: date | None = None,
    close_previous: bool = True,
) -> int:
    """Validate and insert a new version, returning how many rows were written.

    Refuses to touch a version that already exists. When close_previous is set,
    every open earlier version is ended on the day the new one starts, so the
    two never overlap and the old rows stay readable.
    """
    validate_actions(actions)

    if session.scalar(select(func.count()).where(AgentActionCatalog.version == version)):
        raise ActionCatalogValidationError(
            f"version {version} already exists and may not be changed"
        )

    if close_previous:
        session.execute(
            update(AgentActionCatalog)
            .where(
                AgentActionCatalog.version < version,
                AgentActionCatalog.valid_to.is_(None),
            )
            .values(valid_to=valid_from)
        )

    session.add_all(
        AgentActionCatalog(
            version=version,
            action_code=spec.action_code,
            title=spec.title,
            who=spec.who,
            evidence_required=spec.evidence_required,
            response_kind=spec.response_kind,
            message_angle=spec.message_angle,
            channel=spec.channel,
            content_mix=spec.content_mix,
            default_permission=spec.default_permission,
            money_ceiling_kes=spec.money_ceiling_kes,
            paused=spec.paused,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        for spec in actions
    )
    session.flush()
    return len(actions)


def active_action_catalog_version(session: Session, at: date) -> int | None:
    """The version in force on `at`, or None if there is none."""
    return session.scalar(
        select(AgentActionCatalog.version)
        .where(
            AgentActionCatalog.valid_from <= at,
            or_(AgentActionCatalog.valid_to.is_(None), AgentActionCatalog.valid_to > at),
        )
        .order_by(AgentActionCatalog.valid_from.desc(), AgentActionCatalog.version.desc())
        .limit(1)
    )


def load_active_actions(session: Session, at: date) -> dict[str, AgentActionCatalog]:
    """Every action in the version in force on `at`, keyed by action code."""
    version = active_action_catalog_version(session, at)
    if version is None:
        return {}
    rows = session.scalars(
        select(AgentActionCatalog)
        .where(AgentActionCatalog.version == version)
        .order_by(AgentActionCatalog.catalog_id)
    ).all()
    return {row.action_code: row for row in rows}


def load_action(session: Session, action_code: str, at: date) -> AgentActionCatalog | None:
    """One action as it stood in the version in force on `at`."""
    return load_active_actions(session, at).get(action_code)


def action_is_paused(session: Session, action_code: str, at: date) -> bool:
    """Whether this action is stopped right now.

    An action with no row in the version in force counts as paused: the agent
    may only choose from what the catalogue currently offers.
    """
    row = load_action(session, action_code, at)
    return row is None or row.paused


def selectable_actions(session: Session, at: date) -> dict[str, AgentActionCatalog]:
    """The actions the agent may choose from on `at`, with paused ones removed."""
    return {code: row for code, row in load_active_actions(session, at).items() if not row.paused}
