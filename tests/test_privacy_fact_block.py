"""The closed fact block: what may reach the model, and how it is shaped.

Every guarantee here is structural, not a convention someone has to remember:
an unknown key is rejected at construction, a band outside the real
vocabulary is rejected, an amount is rounded before it can be read back out.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.privacy.fact_block import MODEL_FACT_BLOCK_VERSION, ModelFactBlock, round_sig_figs
from app.transform.features import (
    CADENCE_BANDS,
    EXIT_REASONS,
    FUND_TYPES,
    HOLD_BANDS,
    PURCHASE_DEPTHS,
    RECENCY_BANDS,
    TREND_BANDS,
    VALUE_BANDS,
)

# Every field the design permits, and nothing else.
PERMITTED_KEYS = {
    "recency_band",
    "value_band",
    "cadence_band",
    "hold_band",
    "purchase_depth",
    "trend_band",
    "exit_reason",
    "fund_type",
    "in_wave",
    "has_depth",
    "staged_exit",
    "stale_contact",
    "fund_name",
    "years_since_exit",
    "typical_contribution_kes",
    "largest_contribution_kes",
    "invested_every_n_days",
    "days_held_after_last_topup",
    "month_they_left",
}


def test_the_schema_carries_exactly_the_permitted_keys() -> None:
    assert set(ModelFactBlock.model_fields) == PERMITTED_KEYS


def test_an_unknown_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        ModelFactBlock(client_name="Jane Doe")


def test_round_sig_figs_rounds_correctly() -> None:
    assert round_sig_figs(None) is None
    assert round_sig_figs(0) == 0
    assert round_sig_figs(149832.50, sig_figs=2) == 150000
    assert round_sig_figs(4466000, sig_figs=2) == 4500000
    assert round_sig_figs(3661, sig_figs=2) == 3700
    assert round_sig_figs(10750, sig_figs=2) == 11000
    assert round_sig_figs(44.12, sig_figs=2) == 44


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (1, 1), (150_000, 150_000), (44_120, 44_000), (9_999, 10_000)],
)
def test_rounding_keeps_exactly_two_significant_figures(value, expected) -> None:
    assert round_sig_figs(value) == expected


def test_an_exact_amount_never_survives_construction() -> None:
    block = ModelFactBlock(typical_contribution_kes=4_466_000)
    assert block.typical_contribution_kes != 4_466_000
    assert block.typical_contribution_kes == 4_500_000


def test_modelfactblock_rounds_monetary_amounts_and_years() -> None:
    block = ModelFactBlock(
        typical_contribution_kes=149832,
        largest_contribution_kes=4466000,
        years_since_exit=2.54,
    )
    assert block.typical_contribution_kes == 150000
    assert block.largest_contribution_kes == 4500000
    assert block.years_since_exit == 2.5


@pytest.mark.parametrize("cadence_band", [None, "None"])
def test_a_client_with_no_cadence_cannot_have_one_quoted(cadence_band) -> None:
    block = ModelFactBlock(cadence_band=cadence_band, invested_every_n_days=30)
    assert block.invested_every_n_days is None


def test_a_client_with_a_real_cadence_keeps_it() -> None:
    block = ModelFactBlock(cadence_band="Tight", invested_every_n_days=30)
    assert block.invested_every_n_days == 30


def test_modelfactblock_month_they_left_validation() -> None:
    block = ModelFactBlock(month_they_left="2024-03")
    assert block.month_they_left == "2024-03"
    with pytest.raises(ValidationError, match="YYYY-MM"):
        ModelFactBlock(month_they_left="2024-03-15")


@pytest.mark.parametrize("value", ["2024-7", "July 2024", "2024/07"])
def test_month_they_left_rejects_anything_else(value) -> None:
    with pytest.raises(ValidationError):
        ModelFactBlock(month_they_left=value)


@pytest.mark.parametrize(
    ("field", "domain"),
    [
        ("recency_band", RECENCY_BANDS),
        ("value_band", VALUE_BANDS),
        ("cadence_band", CADENCE_BANDS),
        ("hold_band", HOLD_BANDS),
        ("purchase_depth", PURCHASE_DEPTHS),
        ("trend_band", TREND_BANDS),
        ("exit_reason", EXIT_REASONS),
        ("fund_type", FUND_TYPES),
    ],
)
def test_every_band_field_accepts_only_its_real_vocabulary(field, domain) -> None:
    for value in domain:
        ModelFactBlock(**{field: value})
    with pytest.raises(ValidationError):
        ModelFactBlock(**{field: "not-a-real-band-value"})


def test_modelfactblock_to_dict_omits_none_and_filters_keys() -> None:
    block = ModelFactBlock(
        recency_band="Under 1y",
        fund_name="Cytonn Money Market Fund",
        typical_contribution_kes=150000,
        years_since_exit=1.2,
    )

    full_dict = block.to_dict()
    assert "recency_band" in full_dict
    assert "fund_name" in full_dict
    assert "typical_contribution_kes" in full_dict
    assert "value_band" not in full_dict

    filtered_dict = block.to_dict(permitted_keys=["fund_name", "years_since_exit"])
    assert filtered_dict == {
        "fund_name": "Cytonn Money Market Fund",
        "years_since_exit": 1.2,
    }

    assert block.to_dict(permitted_keys=[]) == {}


def test_a_key_outside_the_schema_cannot_be_smuggled_in_through_narrowing() -> None:
    """permitted_keys filters an already-closed dump, so a bad key just misses."""
    block = ModelFactBlock(fund_name="Cytonn Money Market Fund")
    assert block.to_dict(["client_name", "fund_name"]) == {"fund_name": "Cytonn Money Market Fund"}


def test_the_schema_is_versioned() -> None:
    assert isinstance(MODEL_FACT_BLOCK_VERSION, int)
    assert MODEL_FACT_BLOCK_VERSION >= 1
