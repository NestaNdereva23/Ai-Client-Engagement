"""Template generation limits: effective_limit's pure arithmetic, the
campaign override, and the versioned default it falls back to.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, select

from app.campaigns.template_policy import (
    TemplatePolicyValidationError,
    active_default_config_version,
    effective_limit,
    get_campaign_policy,
    load_active_default_config,
    resolve_effective_limit,
    save_default_config_version,
    set_campaign_policy,
)
from app.db.models.outreach import Campaign
from app.db.models.template_policy import CampaignTemplatePolicy, TemplatePolicyConfigVersion
from app.db.session import SessionLocal


def test_absolute_only_caps_at_the_absolute_value() -> None:
    assert effective_limit(87, max_templates=40, max_templates_pct=None) == 40


def test_percentage_only_caps_at_the_rounded_up_percentage() -> None:
    assert effective_limit(87, max_templates=None, max_templates_pct=50) == 44  # ceil(43.5)


def test_both_set_the_smaller_one_wins() -> None:
    # ceil(87 * 0.5) = 44, smaller than the absolute 40.
    assert effective_limit(87, max_templates=40, max_templates_pct=50) == 40
    # ceil(87 * 0.1) = 9, smaller than the absolute 40.
    assert effective_limit(87, max_templates=40, max_templates_pct=10) == 9


def test_neither_set_is_no_limit() -> None:
    assert effective_limit(87, max_templates=None, max_templates_pct=None) is None


def test_zero_estimate_with_a_percentage_cap_is_zero_not_none() -> None:
    assert effective_limit(0, max_templates=None, max_templates_pct=50) == 0


@pytest.fixture
def campaign(db: None):
    with SessionLocal() as session:
        row = Campaign(name="template policy test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(
            delete(CampaignTemplatePolicy).where(CampaignTemplatePolicy.campaign_id == campaign_id)
        )
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_a_campaign_with_no_policy_row_resolves_to_no_limit(campaign: int) -> None:
    with SessionLocal() as session:
        assert get_campaign_policy(session, campaign) is None
        assert resolve_effective_limit(session, campaign, 87) is None


def test_setting_a_policy_is_read_back_and_resolves(campaign: int) -> None:
    with SessionLocal() as session:
        set_campaign_policy(
            session, campaign, max_templates=40, max_templates_pct=None, updated_by="manager-1"
        )
        session.commit()

    with SessionLocal() as session:
        policy = get_campaign_policy(session, campaign)
        assert policy is not None
        assert policy.max_templates == 40
        assert policy.updated_by == "manager-1"
        assert resolve_effective_limit(session, campaign, 87) == 40


def test_setting_a_policy_twice_updates_the_same_row_in_place(campaign: int) -> None:
    with SessionLocal() as session:
        set_campaign_policy(
            session, campaign, max_templates=40, max_templates_pct=None, updated_by="manager-1"
        )
        session.commit()

    with SessionLocal() as session:
        set_campaign_policy(
            session, campaign, max_templates=20, max_templates_pct=None, updated_by="manager-2"
        )
        session.commit()

    with SessionLocal() as session:
        rows = (
            session.execute(
                select(CampaignTemplatePolicy).where(CampaignTemplatePolicy.campaign_id == campaign)
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert (rows[0].max_templates, rows[0].updated_by) == (20, "manager-2")


def test_an_out_of_range_percentage_is_rejected(campaign: int) -> None:
    with SessionLocal() as session:
        with pytest.raises(TemplatePolicyValidationError, match="between 1 and 100"):
            set_campaign_policy(
                session, campaign, max_templates=None, max_templates_pct=101, updated_by="m"
            )


def test_a_non_positive_absolute_cap_is_rejected(campaign: int) -> None:
    with SessionLocal() as session:
        with pytest.raises(TemplatePolicyValidationError, match="positive"):
            set_campaign_policy(
                session, campaign, max_templates=0, max_templates_pct=None, updated_by="m"
            )


@pytest.fixture
def cleanup_default_versions():
    versions: list[int] = []
    yield versions
    with SessionLocal() as session:
        session.execute(
            delete(TemplatePolicyConfigVersion).where(
                TemplatePolicyConfigVersion.version.in_(versions)
            )
        )
        session.commit()


def test_a_campaign_with_no_policy_falls_back_to_the_active_default(
    db: None, campaign: int, cleanup_default_versions
) -> None:
    cleanup_default_versions.append(90101)
    with SessionLocal() as session:
        save_default_config_version(
            session,
            90101,
            default_max_templates=None,
            default_max_templates_pct=25,
            valid_from=date(2020, 1, 1),
        )
        session.commit()

    with SessionLocal() as session:
        assert active_default_config_version(session, date(2026, 1, 1)) == 90101
        default = load_active_default_config(session, date(2026, 1, 1))
        assert default is not None
        assert default.default_max_templates_pct == 25
        assert resolve_effective_limit(session, campaign, 100, at=date(2026, 1, 1)) == 25


def test_a_campaigns_own_policy_wins_over_the_default(
    db: None, campaign: int, cleanup_default_versions
) -> None:
    cleanup_default_versions.append(90102)
    with SessionLocal() as session:
        save_default_config_version(
            session,
            90102,
            default_max_templates=10,
            default_max_templates_pct=None,
            valid_from=date(2020, 1, 1),
        )
        set_campaign_policy(
            session, campaign, max_templates=40, max_templates_pct=None, updated_by="manager-1"
        )
        session.commit()

    with SessionLocal() as session:
        assert resolve_effective_limit(session, campaign, 87) == 40


def test_a_superseded_default_version_is_not_the_active_one(
    db: None, cleanup_default_versions
) -> None:
    cleanup_default_versions.extend([90103, 90104])
    with SessionLocal() as session:
        save_default_config_version(
            session,
            90103,
            default_max_templates=10,
            default_max_templates_pct=None,
            valid_from=date(2020, 1, 1),
            valid_to=date(2025, 1, 1),
        )
        save_default_config_version(
            session,
            90104,
            default_max_templates=20,
            default_max_templates_pct=None,
            valid_from=date(2025, 1, 1),
        )
        session.commit()

    with SessionLocal() as session:
        assert active_default_config_version(session, date(2024, 6, 1)) == 90103
        assert active_default_config_version(session, date(2026, 1, 1)) == 90104


def test_saving_a_version_that_already_exists_is_refused(
    db: None, cleanup_default_versions
) -> None:
    cleanup_default_versions.append(90105)
    with SessionLocal() as session:
        save_default_config_version(
            session,
            90105,
            default_max_templates=10,
            default_max_templates_pct=None,
            valid_from=date(2020, 1, 1),
        )
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(TemplatePolicyValidationError, match="already exists"):
            save_default_config_version(
                session,
                90105,
                default_max_templates=20,
                default_max_templates_pct=None,
                valid_from=date(2021, 1, 1),
            )
