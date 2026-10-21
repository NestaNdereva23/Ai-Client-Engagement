from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.privacy.fact_block import ProposalFactBlock
from app.privacy.scanners import InboundLeak, scan_inbound

PERMITTED_KEYS = {
    "group_name",
    "action_code",
    "action_title",
    "action_who",
    "client_count",
    "money_total_kes",
    "evidence",
    "evidence_detail",
}


def _block() -> ProposalFactBlock:
    return ProposalFactBlock(
        group_name="deposit_size_falling",
        action_code="ask_what_changed",
        action_title="Ask what changed",
        action_who="Someone whose deposits have been getting smaller",
        client_count=47,
        money_total_kes=12_000_000,
        evidence="47 clients, KES 12,000,000 held",
        evidence_detail={"client_count": 47},
    )


def test_the_schema_carries_exactly_the_permitted_keys() -> None:
    assert set(ProposalFactBlock.model_fields) == PERMITTED_KEYS


def test_a_field_outside_the_schema_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProposalFactBlock(**{**_block().to_dict(), "client_name": "Wanjiru Kamau"})


def test_an_unrounded_money_total_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProposalFactBlock(**{**_block().to_dict(), "money_total_kes": 12_345_678})


def test_a_built_block_passes_the_inbound_scan() -> None:
    scan_inbound(_block().to_dict())


def test_an_extra_key_added_by_hand_fails_the_inbound_scan() -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({**_block().to_dict(), "client_name": "Wanjiru Kamau"})
