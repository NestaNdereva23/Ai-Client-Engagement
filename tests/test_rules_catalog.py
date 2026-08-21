"""The angle catalogue: what ships in it, and that a shipped version stays put."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, select

from app.db.models.rules import MessageAngleCatalog
from app.db.session import SessionLocal
from app.rules.catalog import (
    AngleSpec,
    CatalogValidationError,
    active_catalog_version,
    load_active_angles,
    load_angle,
    save_catalog_version,
    validate_angles,
)

# The twelve the campaign ships with, in the order the rules are applied.
SEEDED_ANGLES = (
    "not_a_goodbye",
    "wrong_shelf",
    "see_what_changed",
    "the_long_hold",
    "your_next_deposit",
    "second_try",
    "you_wound_down",
    "you_were_scaling",
    "you_were_fading",
    "back_on_schedule",
    "onboarding_retry",
    "pick_up_again",
)

SEEDED_VERSION = 1
IN_FORCE = date(2026, 8, 2)

# version 2 superseded version 1 on 2026-08-21 and, unlike the fixtures the
# tests below add and remove, has no valid_to: it is the baseline for any
# date from then on that a test does not give a more specific override.
ACTIVE_VERSION = 2


def _spec(angle: str = "trial_angle", **overrides: str) -> AngleSpec:
    fields = {
        "angle": angle,
        "headline": "A headline",
        "who": "Who it addresses",
        "claim": "What may be claimed",
        "ask": "What it asks for",
        "never": "What it must never say",
        "use": "How RAG facts may be used",
    }
    fields.update(overrides)
    return AngleSpec(**fields)


@pytest.fixture
def catalog_versions():
    """Remove any catalogue versions a test writes, leaving the seed alone."""
    versions: list[int] = []
    yield versions
    if not versions:
        return
    with SessionLocal() as session:
        session.execute(
            delete(MessageAngleCatalog).where(MessageAngleCatalog.version.in_(versions))
        )
        session.commit()


# --- validation, no database needed ---


def test_a_catalogue_may_not_be_empty() -> None:
    with pytest.raises(CatalogValidationError, match="may not be empty"):
        validate_angles([])


def test_angle_identifiers_must_be_unique() -> None:
    with pytest.raises(CatalogValidationError, match="unique"):
        validate_angles([_spec("same"), _spec("same")])


@pytest.mark.parametrize("field", ["angle", "headline", "who", "claim", "ask", "never", "use"])
def test_every_field_must_carry_something(field: str) -> None:
    with pytest.raises(CatalogValidationError, match=f"empty '{field}'"):
        validate_angles([_spec(**{field: "   "})])


def test_a_well_formed_catalogue_passes() -> None:
    validate_angles([_spec("one"), _spec("two")])


# --- the seeded catalogue ---


def test_the_seed_ships_all_twelve_angles(db: None) -> None:
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)
    assert set(angles) == set(SEEDED_ANGLES)


def test_the_seed_keeps_the_order_the_rules_are_applied_in(db: None) -> None:
    with SessionLocal() as session:
        rows = session.scalars(
            select(MessageAngleCatalog)
            .where(MessageAngleCatalog.version == SEEDED_VERSION)
            .order_by(MessageAngleCatalog.catalog_id)
        ).all()
    assert tuple(row.angle for row in rows) == SEEDED_ANGLES


def test_every_seeded_angle_carries_a_full_brief(db: None) -> None:
    """A blank field would silently drop part of the brief from the prompt."""
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)
    for angle, row in angles.items():
        for field in ("headline", "who", "claim", "ask", "never"):
            assert str(getattr(row, field)).strip(), f"{angle} has an empty {field}"


def test_the_seeded_catalogue_would_pass_its_own_validation(db: None) -> None:
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)
    # Version 1 predates use, so it is backfilled here rather than read off
    # the row: this checks the shape of the rest of the brief round-trips,
    # not that a pre-use version somehow already had one.
    validate_angles(
        [
            AngleSpec(
                row.angle,
                row.headline,
                row.who,
                row.claim,
                row.ask,
                row.never,
                use=row.use or "not recorded for this version",
            )
            for row in angles.values()
        ]
    )


def test_every_prohibition_is_phrased_as_one(db: None) -> None:
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)
    assert all("never" in row.never.lower() for row in angles.values())


def test_one_angle_reads_exactly_as_written(db: None) -> None:
    """Spot check the wording, since the brief is what conditions the model."""
    with SessionLocal() as session:
        row = load_angle(session, "back_on_schedule", IN_FORCE)
    assert row is not None
    assert row.headline == "Restart the standing order"
    assert row.claim == "A genuine, measurable savings rhythm that stopped"
    assert row.never.startswith("Never state an exact purchase count")


# --- versioning ---


def test_a_shipped_version_may_not_be_edited(db: None) -> None:
    with (
        SessionLocal() as session,
        pytest.raises(CatalogValidationError, match="may not be mutated"),
    ):
        save_catalog_version(session, SEEDED_VERSION, [_spec()], valid_from=date(2026, 9, 1))


def test_a_later_version_supersedes_the_one_before(db: None, catalog_versions) -> None:
    catalog_versions.append(99)
    later = date(2026, 9, 1)
    with SessionLocal() as session:
        save_catalog_version(session, 99, [_spec("only_angle")], valid_from=later)
        session.commit()

    with SessionLocal() as session:
        assert active_catalog_version(session, later) == 99
        assert set(load_active_angles(session, later)) == {"only_angle"}
        # The day before it starts, the seeded catalogue is still the live one.
        assert active_catalog_version(session, IN_FORCE) == SEEDED_VERSION


def test_a_version_that_has_ended_is_not_active(db: None, catalog_versions) -> None:
    catalog_versions.append(98)
    with SessionLocal() as session:
        save_catalog_version(
            session,
            98,
            [_spec("expired")],
            valid_from=date(2026, 9, 1),
            valid_to=date(2026, 9, 10),
        )
        session.commit()

    with SessionLocal() as session:
        assert active_catalog_version(session, date(2026, 9, 5)) == 98
        assert active_catalog_version(session, date(2026, 9, 20)) == ACTIVE_VERSION


def test_an_invalid_catalogue_is_not_written(db: None, catalog_versions) -> None:
    catalog_versions.append(97)
    with SessionLocal() as session:
        with pytest.raises(CatalogValidationError):
            save_catalog_version(session, 97, [_spec(headline="")], valid_from=date(2026, 9, 1))
        session.rollback()

    with SessionLocal() as session:
        assert active_catalog_version(session, date(2026, 9, 1)) == ACTIVE_VERSION


def test_an_unknown_angle_resolves_to_nothing(db: None) -> None:
    with SessionLocal() as session:
        assert load_angle(session, "no_such_angle", IN_FORCE) is None


def test_nothing_is_active_before_the_catalogue_starts(db: None) -> None:
    with SessionLocal() as session:
        assert active_catalog_version(session, date(2020, 1, 1)) is None
        assert load_active_angles(session, date(2020, 1, 1)) == {}
