"""Tests for the active-clients response contract models."""

from __future__ import annotations

from app.ingestion.contracts_active import schema_drift_active


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


def test_schema_drift_accepts_meta():
    payload = _sample_payload()
    payload["meta"] = {"total": 1, "current_page": 1, "last_page": 1}
    assert schema_drift_active(payload) == set()
