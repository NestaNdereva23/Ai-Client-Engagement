"""RiskFactBlock (AM15): the closed, band-only fact block a briefing
narrative may see. Same structural guarantees as ModelFactBlock
(tests/test_privacy_fact_block.py), applied to the active-book risk
briefing's own vocabulary instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.privacy.fact_block import (
    DEPOSIT_TREND_BANDS,
    FUND_DISPLAY_NAMES,
    RISK_FACT_BLOCK_VERSION,
    RiskFactBlock,
)
from app.risk.routing import ROUTES
from app.risk.scoring import RISK_BANDS
from app.transform.active_features import BALANCE_TIERS, RECENCY_BANDS, VALUE_TIERS

# Every field the design permits, and nothing else.
PERMITTED_KEYS = {
    "risk_band",
    "route",
    "balance_tier",
    "recency_band",
    "value_tier",
    "deposit_trend_band",
    "fund_name",
    "sig_heavy_withdrawal",
    "sig_dormant",
    "sig_broken_pattern",
    "sig_shrinking",
    "sig_going_dormant",
    "sig_never_repeated",
    "deposit_count_capped",
    "withdrawal_history_hidden",
    "holds_both_funds",
    "has_open_complaint",
}


def test_the_schema_carries_exactly_the_permitted_keys() -> None:
    assert set(RiskFactBlock.model_fields) == PERMITTED_KEYS


def test_version_constant_is_set() -> None:
    assert RISK_FACT_BLOCK_VERSION == 1


def test_an_unknown_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        RiskFactBlock(client_name="Jane Doe")


def test_a_client_id_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        RiskFactBlock(client_id=94001)


def test_an_exact_score_or_amount_has_no_field_to_carry_it() -> None:
    # risk_score, balance, and every KES figure are deliberately absent from
    # the schema entirely -- not merely rejected as an out-of-band value,
    # since RiskFactBlock never declares a numeric field at all.
    with pytest.raises(ValidationError):
        RiskFactBlock(risk_score=60)
    with pytest.raises(ValidationError):
        RiskFactBlock(balance=1_000_000.0)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("risk_band", "Severe"),
        ("route", "mystery_queue"),
        ("balance_tier", "Unknown"),
        ("recency_band", "5-6m"),
        ("value_tier", "Huge"),
        ("deposit_trend_band", "sideways"),
        ("fund_name", "Some Other Fund"),
    ],
)
def test_a_band_field_rejects_a_value_outside_its_vocabulary(field, bad_value) -> None:
    with pytest.raises(ValidationError):
        RiskFactBlock(**{field: bad_value})


@pytest.mark.parametrize(
    ("field", "good_value"),
    [
        ("risk_band", next(iter(RISK_BANDS))),
        ("route", ROUTES[0]),
        ("balance_tier", BALANCE_TIERS[0]),
        ("recency_band", RECENCY_BANDS[0]),
        ("value_tier", VALUE_TIERS[0]),
        ("deposit_trend_band", DEPOSIT_TREND_BANDS[0]),
        ("fund_name", next(iter(FUND_DISPLAY_NAMES.values()))),
    ],
)
def test_a_band_field_accepts_its_own_vocabulary(field, good_value) -> None:
    block = RiskFactBlock(**{field: good_value})
    assert getattr(block, field) == good_value


def test_booleans_pass_through_unchanged() -> None:
    block = RiskFactBlock(
        sig_dormant=True,
        sig_heavy_withdrawal=False,
        has_open_complaint=True,
        holds_both_funds=False,
    )
    assert block.sig_dormant is True
    assert block.sig_heavy_withdrawal is False
    assert block.has_open_complaint is True
    assert block.holds_both_funds is False


def test_to_dict_drops_unset_fields() -> None:
    block = RiskFactBlock(risk_band="Watch", sig_dormant=True)
    assert block.to_dict() == {"risk_band": "Watch", "sig_dormant": True}


def test_to_dict_narrows_to_permitted_keys_only() -> None:
    block = RiskFactBlock(risk_band="Watch", sig_dormant=True)
    assert block.to_dict(permitted_keys=["risk_band"]) == {"risk_band": "Watch"}
