from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.privacy.fact_block import AssistantFactBlock
from app.privacy.scanners import InboundLeak, scan_inbound

PERMITTED_KEYS = {
    "risk_band",
    "balance_tier",
    "value_tier",
    "recency_band",
    "deposit_trend_band",
    "deposit_rhythm_band",
    "largest_withdrawal_size_band",
    "last_withdrawal_recency_band",
    "fund_name",
    "sig_heavy_withdrawal",
    "sig_dormant",
    "sig_broken_pattern",
    "sig_shrinking",
    "near_preservation_line",
    "sig_never_repeated",
    "holds_both_funds",
    "last_contact_channel",
    "last_contact_outcome",
    "last_contact_age_band",
    "contacts_last_90_days_band",
}


def _block() -> AssistantFactBlock:
    return AssistantFactBlock(
        risk_band="Watch",
        balance_tier="Premium",
        deposit_rhythm_band="every_two_months",
        largest_withdrawal_size_band="large",
        last_withdrawal_recency_band="this_week",
        sig_heavy_withdrawal=True,
        last_contact_channel="none",
        last_contact_age_band="never",
    )


def test_the_schema_carries_exactly_the_permitted_keys() -> None:
    assert set(AssistantFactBlock.model_fields) == PERMITTED_KEYS


@pytest.mark.parametrize("key", ["client_name", "client_id", "client_code", "note", "balance"])
def test_a_field_with_no_band_is_rejected(key) -> None:
    with pytest.raises(ValidationError):
        AssistantFactBlock(**{key: "x"})


def test_a_value_outside_its_band_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AssistantFactBlock(largest_withdrawal_size_band="KSh 1,050,600")


def test_a_built_block_passes_the_inbound_scan() -> None:
    scan_inbound(_block().to_dict())


def test_an_extra_key_added_by_hand_fails_the_inbound_scan() -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({**_block().to_dict(), "client_name": "Wanjiru Kamau"})


def test_a_band_value_made_up_by_hand_fails_the_inbound_scan() -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({**_block().to_dict(), "last_contact_channel": "called on 2 October"})
