from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.campaigns.preview import preview_cohort, preview_cohort_batch
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal

FUND_ID = 97770


@pytest.fixture
def cohort(db: None):
    matching_a, matching_b, non_matching = 97771, 97772, 97773
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Preview Test Fund"))
        session.commit()
        for client_id, amount in (
            (matching_a, 100_000.0),
            (matching_b, 50_000.0),
            (non_matching, 25_000.0),
        ):
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                    total_purchase_amount=amount,
                )
            )
        session.commit()
        session.add_all(
            [
                ClientFeatures(client_id=matching_a, value_band="Medium", purchase_depth="capped"),
                ClientFeatures(client_id=matching_b, value_band="Medium", purchase_depth="capped"),
                ClientFeatures(client_id=non_matching, value_band="Low", purchase_depth="capped"),
            ]
        )
        session.add_all(
            [
                ClientMessageIndicators(
                    client_id=matching_a,
                    message_angle="pick_up_again",
                    urgency="normal",
                    priority_tier="T2",
                    prompt_variant="pick_up_again",
                    rule_name="preview_test",
                    rule_version=1,
                ),
                ClientMessageIndicators(
                    client_id=matching_b,
                    message_angle="your_next_deposit",
                    urgency="normal",
                    priority_tier="T2",
                    prompt_variant="your_next_deposit",
                    rule_name="preview_test",
                    rule_version=1,
                ),
            ]
        )
        session.add_all(
            [
                PiiVault(client_id=matching_a, client_name="Preview Alpha"),
                PiiVault(client_id=matching_b, client_name="Preview Beta"),
                PiiVault(client_id=non_matching, client_name="Preview Gamma"),
            ]
        )
        session.commit()

    yield matching_a, matching_b, non_matching

    with SessionLocal() as session:
        session.execute(
            delete(ClientMessageIndicators).where(
                ClientMessageIndicators.client_id.in_((matching_a, matching_b, non_matching))
            )
        )
        session.execute(
            delete(ClientFeatures).where(
                ClientFeatures.client_id.in_((matching_a, matching_b, non_matching))
            )
        )
        session.execute(
            delete(PiiVault).where(PiiVault.client_id.in_((matching_a, matching_b, non_matching)))
        )
        session.execute(
            delete(Clients).where(Clients.client_id.in_((matching_a, matching_b, non_matching)))
        )
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


def test_preview_cohort_matches_the_filter(cohort) -> None:
    matching_a, matching_b, non_matching = cohort
    with SessionLocal() as session:
        preview = preview_cohort(session, {"fund_id": FUND_ID, "value_band": "Medium"})
    assert preview.matched_count == 2
    assert preview.primary_count == 2
    assert preview.suppressed_count == 0
    assert preview.valued_count == 2
    assert preview.estimated_value == 150_000.0


def test_preview_cohort_dedups_same_person(cohort) -> None:
    matching_a, matching_b, non_matching = cohort
    with SessionLocal() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_((matching_a, matching_b))))
        session.add_all(
            [
                PiiVault(client_id=matching_a, client_name="Same Preview Person"),
                PiiVault(client_id=matching_b, client_name="Same Preview Person"),
            ]
        )
        session.commit()

        preview = preview_cohort(session, {"fund_id": FUND_ID, "value_band": "Medium"})
    assert preview.matched_count == 2
    assert preview.primary_count == 1
    assert preview.suppressed_count == 1
    assert preview.valued_count == 1
    assert preview.estimated_value == 100_000.0


def test_preview_cohort_with_no_matches_is_all_zero(db: None) -> None:
    with SessionLocal() as session:
        preview = preview_cohort(session, {"fund_id": 999999999})
    assert preview.matched_count == 0
    assert preview.primary_count == 0
    assert preview.suppressed_count == 0
    assert preview.valued_count == 0
    assert preview.estimated_value == 0.0


def test_preview_cohort_batch_scopes_each_angle(cohort) -> None:
    matching_a, matching_b, non_matching = cohort
    with SessionLocal() as session:
        result = preview_cohort_batch(
            session,
            {"fund_id": FUND_ID, "value_band": "Medium"},
            ["pick_up_again", "your_next_deposit", "back_on_schedule"],
        )
    assert result.narrow.matched_count == 2
    assert result.narrow.estimated_value == 150_000.0

    by_angle = {a.message_angle: a for a in result.angles}
    assert by_angle["pick_up_again"].matched_count == 1
    assert by_angle["pick_up_again"].estimated_value == 100_000.0
    assert by_angle["your_next_deposit"].matched_count == 1
    assert by_angle["your_next_deposit"].estimated_value == 50_000.0
    assert by_angle["back_on_schedule"].matched_count == 0
    assert by_angle["back_on_schedule"].estimated_value == 0.0


def test_preview_cohort_batch_with_no_angles_returns_just_the_narrow_count(cohort) -> None:
    with SessionLocal() as session:
        result = preview_cohort_batch(session, {"fund_id": FUND_ID, "value_band": "Medium"}, [])
    assert result.narrow.matched_count == 2
    assert result.angles == []
