"""Tests for the response contract models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ingestion.contracts import ClientRecord, FundRecord, RawEnvelope, schema_drift


def _sample_payload():
    return {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market",
                "inactive_client_count": 2,
                "clients": [
                    {
                        "client_id": 1,
                        "client_code": "C-1",
                        "client_name": "Jane Doe",
                        "balance": 0,
                        "computed_at": "2026-07-01T00:00:00Z",
                        "last_5_purchases": [
                            {
                                "id": 99,
                                "date": "2025-01-02",
                                "number": "15000.50",
                                "unit_fund_id": 10,
                                "unit_price": 1.0,
                                "fees_incurred": 0,
                                "unit_fund": {"redundant": True},
                            }
                        ],
                        "last_2_sales": [],
                    }
                ],
            }
        ]
    }


def test_amounts_and_dates_kept_as_strings():
    fund = FundRecord.model_validate(_sample_payload()["data"][0])
    client = ClientRecord.model_validate(fund.clients[0])
    txn = client.last_5_purchases[0]
    assert txn.number == "15000.50"
    assert txn.date == "2025-01-02"


def test_string_amount_and_client_code_variants():
    # number arrives as a real number, client_code as an int
    client = ClientRecord.model_validate(
        {
            "client_id": "7",
            "client_code": 42,
            "last_5_purchases": [{"id": 1, "number": 2500, "date": "2025-06-01T10:00:00+03:00"}],
        }
    )
    assert client.client_id == 7
    assert client.client_code == 42
    assert client.last_5_purchases[0].number == "2500"


def test_missing_client_id_is_rejected():
    with pytest.raises(ValidationError) as err:
        ClientRecord.model_validate({"client_code": "C-2"})
    assert err.value.errors()[0]["loc"] == ("client_id",)


def test_fund_requires_unit_fund_id():
    with pytest.raises(ValidationError):
        FundRecord.model_validate({"unit_fund_name": "No id"})


def test_bad_client_does_not_fail_the_fund():
    # The fund keeps clients as raw dicts, so one bad client does not break parsing.
    fund = FundRecord.model_validate(
        {"unit_fund_id": 5, "clients": [{"client_code": "no-id"}, {"client_id": 3}]}
    )
    assert len(fund.clients) == 2


def test_schema_drift_clean_and_dirty():
    assert schema_drift(_sample_payload()) == set()
    dirty = {
        "data": [{"unit_fund_id": 1, "surprise": 9, "clients": [{"client_id": 2, "extra": 1}]}]
    }
    assert schema_drift(dirty) == {"surprise", "extra"}


def test_envelope_defaults_to_empty():
    assert RawEnvelope.model_validate({}).data == []
