"""Pattern and literal blocking on the inbound and outbound scanners.

Inbound blocks a real value that slipped into an allow-listed field; outbound
blocks a draft that echoes an identifier. The full bucket vocabulary must pass
so the detectors never false-fire on legitimate context.
"""

from __future__ import annotations

import pytest

from app.privacy.scanners import InboundLeak, OutboundLeak, scan_inbound, scan_outbound

# Every label the model-facing buckets can take. None may trip a detector.
BUCKET_VALUES = [
    "None observed",
    "One-and-done",
    "Occasional (2-4)",
    "Frequent (5+, censored)",
    "Unknown",
    "Exited under 1y",
    "Exited 1 to 2y",
    "Exited 2 to 3y",
    "Exited 3y plus",
    "Top",
    "High",
    "Mid",
    "Low",
    "Regular",
    "Periodic",
    "Infrequent",
]


@pytest.mark.parametrize("value", BUCKET_VALUES)
def test_inbound_passes_the_bucket_vocabulary(value: str) -> None:
    assert scan_inbound({"archetype": value}) is None


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
        scan_inbound({"value_tier_label": leaked})


def test_inbound_blocks_a_known_client_name() -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({"archetype": "One-and-done Wangari"}, identifiers=["Wangari"])


def test_inbound_still_rejects_offlist_keys() -> None:
    with pytest.raises(InboundLeak, match="allow-list"):
        scan_inbound({"client_name": "Jane"})


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
