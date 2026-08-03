"""What must never reach the model, proven one category at a time.

Each test sends a payload carrying exactly one forbidden thing through the
real boundary entry point, scan_inbound, and asserts it is blocked. This is
the audit trail for the anonymisation checklist: a reviewer can point at one
test per prohibited category rather than trusting the schema by inspection.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.privacy.fact_block import ModelFactBlock
from app.privacy.scanners import InboundLeak, scan_inbound

# A payload otherwise good enough to pass, so each test below adds exactly
# one forbidden thing to it rather than testing an already-broken payload.
_VALID_BASE = {"recency_band": "Under 1y", "fund_name": "Cytonn Money Market Fund"}


def _blocked(**extra: object) -> None:
    with pytest.raises(InboundLeak):
        scan_inbound({**_VALID_BASE, **extra})


def test_client_name_is_forbidden() -> None:
    _blocked(client_name="Jane Doe")


def test_client_code_is_forbidden() -> None:
    _blocked(client_code="C-1001")


def test_client_id_is_forbidden() -> None:
    _blocked(client_id=1001)


def test_a_contact_email_is_forbidden() -> None:
    _blocked(contact_email="jane@example.com")


def test_a_contact_whatsapp_number_is_forbidden() -> None:
    _blocked(contact_whatsapp="+254712345678")


def test_a_balance_is_forbidden() -> None:
    _blocked(balance=0.0)


def test_an_exact_calendar_date_is_forbidden() -> None:
    """No field takes a full date; only month_they_left, and only as YYYY-MM."""
    _blocked(exit_date="2024-07-15")
    _blocked(last_purchase_date="2024-07-15")
    _blocked(computed_at="2026-07-20T08:00:00")


def test_month_they_left_accepts_only_year_and_month() -> None:
    assert scan_inbound({**_VALID_BASE, "month_they_left": "2024-07"}) is None


@pytest.mark.parametrize(
    "value",
    ["2024-07-15", "2024-07-15T00:00:00", "July 2024", "2024/07"],
)
def test_month_they_left_rejects_anything_more_specific_or_less(value: str) -> None:
    _blocked(month_they_left=value)
    with pytest.raises(ValidationError):
        ModelFactBlock(month_they_left=value)


def test_no_field_on_the_schema_can_hold_a_full_date() -> None:
    """Structural check: month_they_left is the only date-shaped field there is."""
    date_like = [name for name in ModelFactBlock.model_fields if "date" in name.lower()]
    assert date_like == []


def test_no_field_on_the_schema_names_a_balance() -> None:
    balance_like = [name for name in ModelFactBlock.model_fields if "balance" in name.lower()]
    assert balance_like == []


def test_no_field_on_the_schema_names_a_contact_channel() -> None:
    # stale_contact flags that details need verifying; it is not a channel.
    fields = set(ModelFactBlock.model_fields) - {"stale_contact"}
    channel_terms = ("email", "phone", "whatsapp")
    contact_like = [name for name in fields if any(term in name.lower() for term in channel_terms)]
    assert contact_like == []


def test_no_field_on_the_schema_names_an_identifier() -> None:
    id_like = [
        name
        for name in ModelFactBlock.model_fields
        if name in ("client_id", "client_code", "client_name")
    ]
    assert id_like == []


def test_fund_name_cannot_carry_free_text() -> None:
    """fund_name is a display name, not ingested text -- closed, not free-form."""
    _blocked(fund_name="whatever the caller wants to say")
    with pytest.raises(ValidationError):
        ModelFactBlock(fund_name="whatever the caller wants to say")


def test_a_contact_channel_hidden_inside_fund_name_is_still_forbidden() -> None:
    """The one free-text-shaped field in the old design is now closed too."""
    _blocked(fund_name="reach me at jane.doe@example.com or 0712345678")
