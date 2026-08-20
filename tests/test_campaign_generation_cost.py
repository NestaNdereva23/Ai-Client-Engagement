"""Generation cost estimation: the active-rate lookup, and pricing a
campaign's single-generation and template drafting side by side.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.campaigns.generation_cost import (
    GenerationCostConfigMissing,
    UnknownGenerationModel,
    active_generation_cost_config,
    estimate_generation_cost,
    list_generation_cost_models,
)
from app.db.models.campaigns import CampaignStep, Enrollment
from app.db.models.generation_cost import GenerationCostConfigVersion
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal

FUND_ID = 97750
PRIMARY_A, PRIMARY_B, SUPPRESSED = 97751, 97752, 97753


@pytest.fixture
def cleanup_cost_versions():
    versions: list[int] = []
    yield versions
    with SessionLocal() as session:
        session.execute(
            delete(GenerationCostConfigVersion).where(
                GenerationCostConfigVersion.version.in_(versions)
            )
        )
        session.commit()


def test_no_version_covering_the_date_raises(db: None, cleanup_cost_versions) -> None:
    with SessionLocal() as session:
        with pytest.raises(GenerationCostConfigMissing):
            active_generation_cost_config(session, at=date(1999, 1, 1))


def test_an_unsupported_model_raises(db: None, cleanup_cost_versions) -> None:
    with SessionLocal() as session:
        with pytest.raises(UnknownGenerationModel):
            active_generation_cost_config(session, "claude-mythos-5")


def test_the_latest_started_version_wins(db: None, cleanup_cost_versions) -> None:
    cleanup_cost_versions.extend([90301, 90302])
    with SessionLocal() as session:
        session.add_all(
            [
                GenerationCostConfigVersion(
                    version=90301,
                    model="claude-haiku-4-5-20251001",
                    cost_per_generation_usd=0.002877,
                    cost_per_generation_kes=0.37,
                    valid_from=date(2020, 1, 1),
                    valid_to=date(2025, 1, 1),
                ),
                GenerationCostConfigVersion(
                    version=90302,
                    model="claude-haiku-4-5-20251001",
                    cost_per_generation_usd=0.0014,
                    cost_per_generation_kes=0.18,
                    valid_from=date(2025, 1, 1),
                    valid_to=None,
                ),
            ]
        )
        session.commit()

    with SessionLocal() as session:
        old = active_generation_cost_config(session, at=date(2024, 6, 1))
        assert old.version == 90301
        new = active_generation_cost_config(session, at=date(2026, 1, 1))
        assert new.version == 90302


def test_version_numbers_are_scoped_per_model(db: None, cleanup_cost_versions) -> None:
    """Two different models can both be on "version 90304" at once -- the
    version sequence is per model, not global.
    """
    cleanup_cost_versions.append(90304)
    with SessionLocal() as session:
        session.add_all(
            [
                GenerationCostConfigVersion(
                    version=90304,
                    model="claude-haiku-4-5-20251001",
                    cost_per_generation_usd=0.002877,
                    cost_per_generation_kes=0.37,
                    valid_from=date(2020, 1, 1),
                    valid_to=None,
                ),
                GenerationCostConfigVersion(
                    version=90304,
                    model="claude-sonnet-5",
                    cost_per_generation_usd=0.00575,
                    cost_per_generation_kes=0.74,
                    valid_from=date(2020, 1, 1),
                    valid_to=None,
                ),
            ]
        )
        session.commit()

    with SessionLocal() as session:
        haiku = active_generation_cost_config(
            session, "claude-haiku-4-5-20251001", at=date(2026, 1, 1)
        )
        sonnet = active_generation_cost_config(session, "claude-sonnet-5", at=date(2026, 1, 1))
        assert haiku.version == sonnet.version == 90304
        assert haiku.cost_per_generation_usd != sonnet.cost_per_generation_usd


def test_list_generation_cost_models_returns_every_configured_rate(
    db: None, cleanup_cost_versions
) -> None:
    cleanup_cost_versions.append(90305)
    with SessionLocal() as session:
        session.add_all(
            [
                GenerationCostConfigVersion(
                    version=90305,
                    model="claude-haiku-4-5-20251001",
                    cost_per_generation_usd=0.002877,
                    cost_per_generation_kes=0.37,
                    valid_from=date(2020, 1, 1),
                    valid_to=None,
                ),
                GenerationCostConfigVersion(
                    version=90305,
                    model="claude-opus-5",
                    cost_per_generation_usd=0.014375,
                    cost_per_generation_kes=1.85,
                    valid_from=date(2020, 1, 1),
                    valid_to=None,
                ),
            ]
        )
        session.commit()

    with SessionLocal() as session:
        configs = list_generation_cost_models(session, at=date(2026, 1, 1))

    priced_models = {c.model for c in configs}
    assert "claude-haiku-4-5-20251001" in priced_models
    assert "claude-opus-5" in priced_models


@pytest.fixture
def campaign_with_steps(db: None):
    """Two primary enrollments, one suppressed duplicate, and a two-step sequence."""
    with SessionLocal() as session:
        row = Campaign(name="test generation cost campaign")
        session.add(row)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = row.campaign_id

        for client_id in (PRIMARY_A, PRIMARY_B, SUPPRESSED):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.add_all(
            [
                PiiVault(client_id=PRIMARY_A, client_name="Person A"),
                PiiVault(client_id=PRIMARY_B, client_name="Person B"),
                PiiVault(client_id=SUPPRESSED, client_name="Person A"),
            ]
        )
        session.commit()
        session.add_all(
            [
                Enrollment(
                    campaign_id=campaign_id, client_id=PRIMARY_A, is_primary_contact_row=True
                ),
                Enrollment(
                    campaign_id=campaign_id, client_id=PRIMARY_B, is_primary_contact_row=True
                ),
                Enrollment(
                    campaign_id=campaign_id, client_id=SUPPRESSED, is_primary_contact_row=False
                ),
                CampaignStep(
                    campaign_id=campaign_id, step_no=1, offset_days=0, message_angle="pick_up_again"
                ),
                CampaignStep(
                    campaign_id=campaign_id, step_no=2, offset_days=7, message_angle="pick_up_again"
                ),
            ]
        )
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(
            delete(PiiVault).where(PiiVault.client_id.in_((PRIMARY_A, PRIMARY_B, SUPPRESSED)))
        )
        session.execute(
            delete(Clients).where(Clients.client_id.in_((PRIMARY_A, PRIMARY_B, SUPPRESSED)))
        )
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


def test_estimate_prices_enrolled_clients_across_every_step(
    campaign_with_steps: int, cleanup_cost_versions
) -> None:
    cleanup_cost_versions.append(90303)
    with SessionLocal() as session:
        session.add(
            GenerationCostConfigVersion(
                version=90303,
                model="claude-haiku-4-5-20251001",
                cost_per_generation_usd=0.002877,
                cost_per_generation_kes=0.37,
                valid_from=date(2020, 1, 1),
                valid_to=None,
            )
        )
        session.commit()

    with SessionLocal() as session:
        estimate = estimate_generation_cost(session, campaign_with_steps, at=date(2026, 1, 1))

    assert estimate.model == "claude-haiku-4-5-20251001"
    assert estimate.config_version == 90303
    assert estimate.step_count == 2
    # Two primary rows; the suppressed duplicate never sends and isn't priced.
    assert estimate.enrolled_clients == 2

    single = estimate.single_generation
    assert single.count_per_step == 2
    assert single.cost_per_step_usd == pytest.approx(2 * 0.002877)
    assert single.cost_per_step_kes == pytest.approx(2 * 0.37)
    # Total assumes both steps cost the same as the one step priced.
    assert single.total_cost_usd == pytest.approx(single.cost_per_step_usd * 2)
    assert single.total_cost_kes == pytest.approx(single.cost_per_step_kes * 2)

    # No ClientFeatures/indicators are seeded, so no client clears the
    # template-bucket eligibility gate; the template scenario prices at zero
    # rather than raising.
    templates = estimate.templates
    assert templates.count_per_step == 0
    assert templates.total_cost_usd == 0
    assert templates.total_cost_kes == 0


def test_estimate_reprices_the_same_campaign_against_another_model(
    campaign_with_steps: int, cleanup_cost_versions
) -> None:
    cleanup_cost_versions.append(90306)
    with SessionLocal() as session:
        session.add(
            GenerationCostConfigVersion(
                version=90306,
                model="claude-opus-5",
                cost_per_generation_usd=0.014375,
                cost_per_generation_kes=1.85,
                valid_from=date(2020, 1, 1),
                valid_to=None,
            )
        )
        session.commit()

    with SessionLocal() as session:
        estimate = estimate_generation_cost(
            session, campaign_with_steps, model="claude-opus-5", at=date(2026, 1, 1)
        )

    assert estimate.model == "claude-opus-5"
    assert estimate.config_version == 90306
    assert estimate.rate_per_generation_usd == pytest.approx(0.014375)
    single = estimate.single_generation
    assert single.cost_per_step_usd == pytest.approx(2 * 0.014375)
