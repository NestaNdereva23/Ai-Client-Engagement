"""The shared draft/publish lifecycle: save_draft, publish, discard, and the
active_configuration pointer it keeps in step.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.db.models.prompt_config import ActiveConfiguration
from app.db.models.rules import BusinessRule, MessageAngleCatalog, TierContract
from app.db.session import SessionLocal
from app.rules import versioning
from app.rules.catalog import AngleSpec, save_catalog_version
from app.rules.store import RuleSpec, save_version
from app.rules.tier_contract import TierSpec, save_tier_contract_version
from app.rules.versioning import DEFAULT_COMPONENT_KEY, VersioningError

ANGLE = "versioning_test_angle"
TIER = "T4"


def _angle_values(**overrides: object) -> dict:
    values = {
        "headline": "Test headline",
        "who": "Test clients",
        "claim": "A test claim",
        "ask": "A test ask",
        "never": "Never anything untested",
        "use": "Not used in this test",
        "held": False,
    }
    values.update(overrides)
    return values


def _pointer(
    session: Session, component_type: str, component_key: str
) -> ActiveConfiguration | None:
    return session.scalar(
        select(ActiveConfiguration).where(
            ActiveConfiguration.component_type == component_type,
            ActiveConfiguration.component_key == component_key,
        )
    )


def _angle_row_at(session: Session, angle: str, at: date) -> MessageAngleCatalog | None:
    return session.scalar(
        select(MessageAngleCatalog)
        .where(
            MessageAngleCatalog.angle == angle,
            MessageAngleCatalog.valid_from <= at,
            or_(MessageAngleCatalog.valid_to.is_(None), MessageAngleCatalog.valid_to > at),
        )
        .order_by(MessageAngleCatalog.valid_from.desc(), MessageAngleCatalog.version.desc())
        .limit(1)
    )


@pytest.fixture
def cleanup_angle():
    yield ANGLE
    with SessionLocal() as session:
        session.execute(delete(MessageAngleCatalog).where(MessageAngleCatalog.angle == ANGLE))
        session.execute(
            delete(ActiveConfiguration).where(
                ActiveConfiguration.component_type == "message_angle_catalog",
                ActiveConfiguration.component_key == ANGLE,
            )
        )
        session.commit()


@pytest.fixture
def catalog_bulk_versions():
    versions: list[int] = []
    yield versions
    if not versions:
        return
    with SessionLocal() as session:
        session.execute(
            delete(MessageAngleCatalog).where(MessageAngleCatalog.version.in_(versions))
        )
        session.execute(
            delete(ActiveConfiguration).where(
                ActiveConfiguration.component_type == "message_angle_catalog",
                ActiveConfiguration.active_version.in_(versions),
            )
        )
        session.commit()


@pytest.fixture
def tier_bulk_versions():
    versions: list[int] = []
    yield versions
    if not versions:
        return
    with SessionLocal() as session:
        session.execute(delete(TierContract).where(TierContract.version.in_(versions)))
        session.execute(
            delete(ActiveConfiguration).where(
                ActiveConfiguration.component_type == "tier_contract",
                ActiveConfiguration.active_version.in_(versions),
            )
        )
        session.commit()


@pytest.fixture
def rule_bulk_versions():
    versions: list[int] = []
    yield versions
    if not versions:
        return
    with SessionLocal() as session:
        session.execute(delete(BusinessRule).where(BusinessRule.version.in_(versions)))
        session.execute(
            delete(ActiveConfiguration).where(
                ActiveConfiguration.component_type == "business_rules",
                ActiveConfiguration.active_version.in_(versions),
            )
        )
        session.commit()


def test_save_draft_is_not_active(db: None, cleanup_angle: str) -> None:
    with SessionLocal() as session:
        version = versioning.save_draft(
            session, "message_angle_catalog", ANGLE, [_angle_values()], by="tester"
        )
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(MessageAngleCatalog).where(
                MessageAngleCatalog.angle == ANGLE, MessageAngleCatalog.version == version
            )
        )
        assert row is not None
        assert row.status == "draft"
        assert row.valid_from is None
        assert row.created_by == "tester"
        assert _angle_row_at(session, ANGLE, date.today()) is None


def test_publish_activates_and_closes_the_prior_version(db: None, cleanup_angle: str) -> None:
    with SessionLocal() as session:
        v1 = versioning.save_draft(session, "message_angle_catalog", ANGLE, [_angle_values()])
        versioning.publish(
            session, "message_angle_catalog", ANGLE, v1, by="tester", at=date(2026, 9, 1)
        )
        session.commit()

    with SessionLocal() as session:
        pointer = _pointer(session, "message_angle_catalog", ANGLE)
        assert pointer is not None
        assert pointer.active_version == v1

        row = _angle_row_at(session, ANGLE, date(2026, 9, 5))
        assert row is not None
        assert row.version == v1
        assert row.status == "published"
        assert row.published_by == "tester"

    with SessionLocal() as session:
        v2 = versioning.save_draft(
            session,
            "message_angle_catalog",
            ANGLE,
            [_angle_values(headline="A new headline")],
        )
        versioning.publish(session, "message_angle_catalog", ANGLE, v2, at=date(2026, 9, 10))
        session.commit()

    with SessionLocal() as session:
        pointer = _pointer(session, "message_angle_catalog", ANGLE)
        assert pointer.active_version == v2

        current = _angle_row_at(session, ANGLE, date(2026, 9, 10))
        assert current is not None
        assert current.version == v2

        earlier = _angle_row_at(session, ANGLE, date(2026, 9, 5))
        assert earlier is not None
        assert earlier.version == v1

        versions = versioning.list_versions(session, "message_angle_catalog", ANGLE)
        assert [v["version"] for v in versions] == [v1, v2]
        assert versions[0]["status"] == "published"
        assert versions[0]["valid_to"] == date(2026, 9, 10)
        assert versions[1]["valid_to"] is None

        diff = versioning.diff_versions(session, "message_angle_catalog", ANGLE, v1, v2)
        assert diff == {"headline": ("Test headline", "A new headline")}


def test_publish_refuses_a_version_that_is_not_a_draft(db: None, cleanup_angle: str) -> None:
    with SessionLocal() as session:
        v1 = versioning.save_draft(session, "message_angle_catalog", ANGLE, [_angle_values()])
        versioning.publish(session, "message_angle_catalog", ANGLE, v1)
        session.commit()

    with SessionLocal() as session, pytest.raises(VersioningError, match="not a draft"):
        versioning.publish(session, "message_angle_catalog", ANGLE, v1)


def test_publish_refuses_an_unknown_version(db: None, cleanup_angle: str) -> None:
    with SessionLocal() as session, pytest.raises(VersioningError, match="no version"):
        versioning.publish(session, "message_angle_catalog", ANGLE, 999999)


def test_discard_draft_removes_it(db: None, cleanup_angle: str) -> None:
    with SessionLocal() as session:
        version = versioning.save_draft(session, "message_angle_catalog", ANGLE, [_angle_values()])
        session.commit()

    with SessionLocal() as session:
        versioning.discard_draft(session, "message_angle_catalog", ANGLE, version)
        session.commit()

    with SessionLocal() as session:
        assert versioning.list_versions(session, "message_angle_catalog", ANGLE) == []


def test_discard_draft_refuses_a_published_version(db: None, cleanup_angle: str) -> None:
    with SessionLocal() as session:
        version = versioning.save_draft(session, "message_angle_catalog", ANGLE, [_angle_values()])
        versioning.publish(session, "message_angle_catalog", ANGLE, version)
        session.commit()

    with SessionLocal() as session, pytest.raises(VersioningError, match="may not be discarded"):
        versioning.discard_draft(session, "message_angle_catalog", ANGLE, version)


def test_save_catalog_version_still_publishes_directly(
    db: None, catalog_bulk_versions: list[int]
) -> None:
    catalog_bulk_versions.append(999500)
    with SessionLocal() as session:
        save_catalog_version(
            session,
            999500,
            [AngleSpec("versioning_bulk_angle", **_angle_values())],
            valid_from=date(2026, 9, 1),
        )
        session.commit()

    with SessionLocal() as session:
        pointer = _pointer(session, "message_angle_catalog", "versioning_bulk_angle")
        assert pointer is not None
        assert pointer.active_version == 999500


def test_save_tier_contract_version_with_valid_to_does_not_activate(
    db: None, tier_bulk_versions: list[int]
) -> None:
    tier_bulk_versions.append(999501)
    with SessionLocal() as session:
        before = _pointer(session, "tier_contract", TIER)
        before_version = before.active_version if before is not None else None

        save_tier_contract_version(
            session,
            999501,
            [
                TierSpec(
                    tier=TIER,
                    display_name="Test tier",
                    primary_channel="email",
                    max_words=100,
                    sign_off="Test",
                    human_approval=True,
                    review_sample_rate=1.0,
                )
            ],
            valid_from=date(2020, 1, 1),
            valid_to=date(2020, 1, 2),
        )
        session.commit()

    with SessionLocal() as session:
        pointer = _pointer(session, "tier_contract", TIER)
        current_version = pointer.active_version if pointer is not None else None
        assert current_version == before_version


def test_save_version_still_publishes_directly(db: None, rule_bulk_versions: list[int]) -> None:
    rule_bulk_versions.append(999502)
    with SessionLocal() as session:
        save_version(
            session,
            999502,
            [RuleSpec(name="versioning_test_rule", priority=1)],
            valid_from=date(2026, 9, 1),
            validate=False,
        )
        session.commit()

    with SessionLocal() as session:
        pointer = _pointer(session, "business_rules", DEFAULT_COMPONENT_KEY)
        assert pointer is not None
        assert pointer.active_version == 999502
