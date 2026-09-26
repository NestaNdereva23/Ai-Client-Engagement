"""Tests for the active-clients response contract models."""

from __future__ import annotations

from app.ingestion.contracts_active import ActiveClientRecord, schema_drift_active


def _sample_payload():
    return {
        "data": [
            {
                "unit_fund_id": 10,
                "unit_fund_name": "Money Market",
                "client_count": 1,
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
                            }
                        ],
                        "last_2_sales": [
                            {
                                "id": 100,
                                "date": "2025-02-01",
                                "number": "500",
                                "sale_type": "unit_sale",
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_schema_drift_clean_and_dirty():
    assert schema_drift_active(_sample_payload()) == set()
    dirty = {
        "data": [{"unit_fund_id": 1, "surprise": 9, "clients": [{"client_id": 2, "extra": 1}]}]
    }
    assert schema_drift_active(dirty) == {"surprise", "extra"}


def test_new_feed_fields_are_not_drift_and_the_advisor_is_cleaned():
    payload = _sample_payload()
    client = payload["data"][0]["clients"][0]
    client.update(
        fa_name=" Jane Advisor ",
        fa_email=" Jane.Advisor@Cytonn.com ",
        status="active",
        activity_window={"from": "2025-07-01", "to": "2026-07-01"},
        purchases_last_12_months=[{"id": 101, "date": "2026-01-02", "number": "500"}],
        sales_last_12_months=[
            {"id": 102, "date": "2026-02-01", "number": "50", "sale_type": "unit_sale"}
        ],
    )
    assert schema_drift_active(payload) == set()

    record = ActiveClientRecord.model_validate(client)
    assert (record.fa_name, record.fa_email) == ("Jane Advisor", "jane.advisor@cytonn.com")
    assert [t.id for t in record.purchases_last_12_months] == [101]

    client["fa_email"] = "  "
    assert ActiveClientRecord.model_validate(client).fa_email is None


def test_schema_drift_accepts_meta():
    payload = _sample_payload()
    payload["meta"] = {"total": 1, "current_page": 1, "last_page": 1}
    assert schema_drift_active(payload) == set()
