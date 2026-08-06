"""Pattern and literal blocking on the inbound and outbound scanners.

Inbound blocks a real value that slipped into an allow-listed field; outbound
blocks a draft that echoes an identifier. The full bucket vocabulary must pass
so the detectors never false-fire on legitimate context.
"""

from __future__ import annotations

import pytest

from app.privacy.scanners import InboundLeak, OutboundLeak, scan_inbound, scan_outbound
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

# Every label the model-facing bands can take, paired with its own field. None
# may trip a detector.
BUCKET_VALUES = (
    [("recency_band", v) for v in sorted(RECENCY_BANDS)]
    + [("value_band", v) for v in sorted(VALUE_BANDS)]
    + [("cadence_band", v) for v in sorted(CADENCE_BANDS)]
    + [("hold_band", v) for v in sorted(HOLD_BANDS)]
    + [("purchase_depth", v) for v in sorted(PURCHASE_DEPTHS)]
    + [("trend_band", v) for v in sorted(TREND_BANDS)]
    + [("exit_reason", v) for v in sorted(EXIT_REASONS)]
    + [("fund_type", v) for v in sorted(FUND_TYPES)]
)


@pytest.mark.parametrize(("field", "value"), BUCKET_VALUES)
def test_inbound_passes_the_bucket_vocabulary(field: str, value: str) -> None:
    assert scan_inbound({field: value}) is None


@pytest.mark.parametrize(
    "leaked",
    [
        "jane@example.com",  # email
        "0712345678",  # phone
        "1002003004",  # account number
        "KES 1,000,000",  # currency amount
        "250,000.00",  # grouped amount
        "2024-01-01",  # iso date
        "31/12/2023",  # slash date
    ],
)
def test_inbound_blocks_a_real_value_in_an_allowlisted_field(leaked: str) -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({"value_band": leaked})


def test_inbound_blocks_a_known_client_name() -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({"value_band": "Top Wangari"}, identifiers=["Wangari"])


def test_inbound_still_rejects_offlist_keys() -> None:
    with pytest.raises(InboundLeak, match="fact-block"):
        scan_inbound({"client_name": "Jane"})


# --- the wider fact-block path ---


def test_inbound_passes_a_real_fact_block_payload() -> None:
    payload = {
        "recency_band": "Under 1y",
        "fund_name": "Cytonn Money Market Fund",
        "typical_contribution_kes": 150_000,
    }
    assert scan_inbound(payload) is None


def test_inbound_blocks_a_band_value_outside_the_real_vocabulary() -> None:
    with pytest.raises(InboundLeak, match="fact-block"):
        scan_inbound({"recency_band": "made up value", "fund_name": "Cytonn Money Market Fund"})


def test_inbound_blocks_a_key_the_fact_block_does_not_declare() -> None:
    with pytest.raises(InboundLeak, match="fact-block"):
        scan_inbound({"fund_name": "Cytonn Money Market Fund", "client_id": 1001})


def test_inbound_still_blocks_a_literal_identifier_in_a_fact_block_payload() -> None:
    # "Low" is a legitimate value_band and also a real surname, so the literal
    # check must fire on a schema-valid value too, not only a malformed one.
    with pytest.raises(InboundLeak):
        scan_inbound({"value_band": "Low"}, identifiers=["Low"])


def test_an_exact_amount_is_rejected_even_though_it_is_a_valid_int() -> None:
    """4,466,000 type-checks fine; only its rounded form may pass."""
    with pytest.raises(InboundLeak, match="would have corrected"):
        scan_inbound(
            {"typical_contribution_kes": 4_466_000, "fund_name": "Cytonn Money Market Fund"}
        )


def test_an_already_rounded_amount_passes() -> None:
    payload = {"typical_contribution_kes": 4_500_000, "fund_name": "Cytonn Money Market Fund"}
    assert scan_inbound(payload) is None


def test_an_exact_years_since_exit_is_also_rejected() -> None:
    with pytest.raises(InboundLeak, match="would have corrected"):
        scan_inbound({"years_since_exit": 3.047, "fund_name": "Cytonn Money Market Fund"})


def test_a_cadence_fact_with_no_real_cadence_is_rejected() -> None:
    """A caller who bypasses ModelFactBlock cannot smuggle a cadence in."""
    with pytest.raises(InboundLeak, match="would have corrected"):
        scan_inbound(
            {
                "cadence_band": "None",
                "invested_every_n_days": 30,
                "fund_name": "Cytonn Money Market Fund",
            }
        )


def test_a_real_cadence_still_passes() -> None:
    assert (
        scan_inbound(
            {
                "cadence_band": "Tight",
                "invested_every_n_days": 30,
                "fund_name": "Cytonn Money Market Fund",
            }
        )
        is None
    )


def test_outbound_allows_a_placeholder_draft() -> None:
    draft = "Dear {{first_name}}, your {{fund_name}} is waiting. Regards, the team."
    assert scan_outbound(draft, identifiers=["Jane Doe", "Money Market Fund"]) is None


def test_outbound_blocks_a_literal_name_even_with_placeholders_present() -> None:
    with pytest.raises(OutboundLeak):
        scan_outbound("Dear {{first_name}}, this is really for Jane Doe.", identifiers=["Jane Doe"])


@pytest.mark.parametrize("leaked", ["reach me at jane@example.com", "call 0712345678"])
def test_outbound_blocks_a_contact_channel(leaked: str) -> None:
    with pytest.raises(OutboundLeak):
        scan_outbound(leaked)


def test_a_placeholder_is_not_a_literal_even_if_it_names_the_value() -> None:
    # The real first name is Jane; the placeholder token is allowed.
    assert scan_outbound("Hi {{first_name}}", identifiers=["Jane"]) is None
