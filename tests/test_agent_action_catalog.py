"""The action catalogue: what ships in it, and that a shipped version stays put."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, select, update

from app.agents.action_catalog import (
    ActionCatalogValidationError,
    ActionSpec,
    action_is_paused,
    active_action_catalog_version,
    load_action,
    load_active_actions,
    save_action_catalog_version,
    selectable_actions,
    validate_actions,
)
from app.db.models.agent import AgentActionCatalog
from app.db.session import SessionLocal

SEEDED_ACTIONS = (
    "welcome_and_top_up",
    "fee_warning",
    "start_win_back",
    "ask_what_changed",
    "suggest_second_fund",
    "send_learning_note",
    "follow_up_when_no_one_called",
    "do_nothing",
)

SEEDED_VERSION = 1
IN_FORCE = date(2026, 9, 8)


def _spec(action_code: str = "trial_action", **overrides: object) -> ActionSpec:
    fields: dict[str, object] = {
        "action_code": action_code,
        "title": "A title",
        "who": "Who it is for",
        "evidence_required": "What must be true first",
        "content_mix": "balanced",
        "default_permission": "suggest_only",
    }
    fields.update(overrides)
    return ActionSpec(**fields)  # type: ignore[arg-type]


@pytest.fixture
def catalog_versions():
    """Remove any versions a test writes and reopen the seed it closed."""
    versions: list[int] = []
    yield versions
    if not versions:
        return
    with SessionLocal() as session:
        session.execute(delete(AgentActionCatalog).where(AgentActionCatalog.version.in_(versions)))
        session.execute(
            update(AgentActionCatalog)
            .where(AgentActionCatalog.version == SEEDED_VERSION)
            .values(valid_to=None)
        )
        session.commit()


def test_a_catalogue_may_not_be_empty() -> None:
    with pytest.raises(ActionCatalogValidationError, match="may not be empty"):
        validate_actions([])


def test_action_codes_must_be_unique() -> None:
    with pytest.raises(ActionCatalogValidationError, match="unique"):
        validate_actions([_spec("same"), _spec("same")])


@pytest.mark.parametrize("field", ["action_code", "title", "who", "evidence_required"])
def test_every_required_field_must_carry_something(field: str) -> None:
    with pytest.raises(ActionCatalogValidationError, match=f"empty '{field}'"):
        validate_actions([_spec(**{field: "   "})])


def test_an_unknown_content_mix_is_refused() -> None:
    with pytest.raises(ActionCatalogValidationError, match="content mix"):
        validate_actions([_spec(content_mix="chatty")])


def test_an_unknown_permission_level_is_refused() -> None:
    with pytest.raises(ActionCatalogValidationError, match="permission level"):
        validate_actions([_spec(default_permission="whenever_it_likes")])


def test_a_money_ceiling_must_be_above_zero() -> None:
    with pytest.raises(ActionCatalogValidationError, match="above zero"):
        validate_actions([_spec(money_ceiling_kes=0.0)])


def test_an_angle_without_a_channel_is_refused() -> None:
    with pytest.raises(ActionCatalogValidationError, match="both a message angle"):
        validate_actions([_spec(message_angle="sitting_still")])


def test_an_action_that_sends_nothing_needs_neither() -> None:
    validate_actions([_spec("quiet")])


def test_a_well_formed_catalogue_passes() -> None:
    validate_actions([_spec("one", message_angle="sitting_still", channel="email"), _spec("two")])


def test_the_seed_ships_all_eight_actions(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, IN_FORCE)
    assert set(actions) == set(SEEDED_ACTIONS)


def test_the_seed_keeps_the_order_it_was_written_in(db: None) -> None:
    with SessionLocal() as session:
        rows = session.scalars(
            select(AgentActionCatalog)
            .where(AgentActionCatalog.version == SEEDED_VERSION)
            .order_by(AgentActionCatalog.catalog_id)
        ).all()
    assert tuple(row.action_code for row in rows) == SEEDED_ACTIONS


def test_every_seeded_action_starts_at_suggest_only(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, IN_FORCE)
    assert {row.default_permission for row in actions.values()} == {"suggest_only"}


def test_nothing_ships_paused(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, IN_FORCE)
    assert not any(row.paused for row in actions.values())


def test_only_do_nothing_sends_no_message(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, IN_FORCE)
    silent = {code for code, row in actions.items() if row.message_angle is None}
    assert silent == {"do_nothing"}


def test_the_seeded_catalogue_would_pass_its_own_validation(db: None) -> None:
    with SessionLocal() as session:
        actions = load_active_actions(session, IN_FORCE)
    validate_actions(
        [
            ActionSpec(
                action_code=row.action_code,
                title=row.title,
                who=row.who,
                evidence_required=row.evidence_required,
                content_mix=row.content_mix,
                default_permission=row.default_permission,
                message_angle=row.message_angle,
                channel=row.channel,
                money_ceiling_kes=row.money_ceiling_kes,
                paused=row.paused,
            )
            for row in actions.values()
        ]
    )


def test_a_shipped_version_may_not_be_written_over(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(ActionCatalogValidationError, match="already exists"):
            save_action_catalog_version(
                session, SEEDED_VERSION, [_spec()], valid_from=date(2026, 9, 8)
            )
        session.rollback()


def test_a_new_version_closes_the_old_one_and_takes_over(
    db: None, catalog_versions: list[int]
) -> None:
    starts = date(2026, 10, 1)
    with SessionLocal() as session:
        catalog_versions.append(2)
        save_action_catalog_version(session, 2, [_spec("only_action")], valid_from=starts)
        session.commit()

    with SessionLocal() as session:
        assert active_action_catalog_version(session, starts) == 2
        assert set(load_active_actions(session, starts)) == {"only_action"}


def test_the_old_version_is_still_readable_after_a_new_one_lands(
    db: None, catalog_versions: list[int]
) -> None:
    """A proposal made under version one has to stay explainable afterwards."""
    starts = date(2026, 10, 1)
    with SessionLocal() as session:
        catalog_versions.append(2)
        save_action_catalog_version(session, 2, [_spec("only_action")], valid_from=starts)
        session.commit()

    with SessionLocal() as session:
        assert active_action_catalog_version(session, IN_FORCE) == SEEDED_VERSION
        assert set(load_active_actions(session, IN_FORCE)) == set(SEEDED_ACTIONS)


def test_there_is_no_catalogue_before_the_first_one_starts(db: None) -> None:
    with SessionLocal() as session:
        assert active_action_catalog_version(session, date(2020, 1, 1)) is None
        assert load_active_actions(session, date(2020, 1, 1)) == {}


def test_a_paused_action_is_read_back_but_never_offered(
    db: None, catalog_versions: list[int]
) -> None:
    starts = date(2026, 10, 1)
    with SessionLocal() as session:
        catalog_versions.append(2)
        save_action_catalog_version(
            session,
            2,
            [_spec("rested_action", paused=True), _spec("busy_action")],
            valid_from=starts,
        )
        session.commit()

    with SessionLocal() as session:
        assert load_action(session, "rested_action", starts) is not None
        assert action_is_paused(session, "rested_action", starts)
        assert not action_is_paused(session, "busy_action", starts)
        assert set(selectable_actions(session, starts)) == {"busy_action"}


def test_an_action_the_catalogue_does_not_hold_counts_as_paused(db: None) -> None:
    with SessionLocal() as session:
        assert load_action(session, "never_existed", IN_FORCE) is None
        assert action_is_paused(session, "never_existed", IN_FORCE)
