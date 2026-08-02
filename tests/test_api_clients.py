"""The client segment console API: browse buckets, never a name."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

CLIENTS = "/api/v1/clients"
SEGMENTS = "/api/v1/segments"


@pytest.fixture
def two_clients(db: None):
    """Two clients in one fund, different buckets, one with a resolved angle."""
    fund_id = 974
    first_id, second_id = 97401, 97402
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add_all(
            [
                Clients(
                    client_id=first_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                ),
                Clients(
                    client_id=second_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                ),
            ]
        )
        session.commit()
        session.add_all(
            [
                ClientFeatures(
                    client_id=first_id,
                    archetype="One-and-done",
                    recency_bucket="Exited 1 to 2y",
                    value_tier="High",
                    rhythm_band="Unknown",
                ),
                ClientFeatures(
                    client_id=second_id,
                    archetype="Occasional (2-4)",
                    recency_bucket="Exited 2 to 3y",
                    value_tier="Mid",
                    rhythm_band="Periodic",
                ),
            ]
        )
        session.add(
            ClientMessageIndicators(
                client_id=first_id,
                message_angle="winback_habit",
                urgency="high",
                priority_tier="P1",
                prompt_variant="habit_premium",
                rule_name="frequent",
                rule_version=1,
            )
        )
        session.commit()

    yield first_id, second_id, fund_id

    with SessionLocal() as session:
        session.execute(
            delete(ClientMessageIndicators).where(
                ClientMessageIndicators.client_id.in_([first_id, second_id])
            )
        )
        session.execute(
            delete(ClientFeatures).where(ClientFeatures.client_id.in_([first_id, second_id]))
        )
        session.execute(delete(Clients).where(Clients.client_id.in_([first_id, second_id])))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_list_clients_returns_buckets_and_never_a_name(two_clients) -> None:
    first_id, _second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id})
    assert response.status_code == 200
    items = response.json()["items"]
    ids = [row["client_id"] for row in items]
    assert first_id in ids
    for row in items:
        assert "client_name" not in row
        assert "name" not in row


def test_list_clients_filters_by_archetype(two_clients) -> None:
    first_id, second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id, "archetype": "One-and-done"})
    ids = [row["client_id"] for row in response.json()["items"]]
    assert first_id in ids
    assert second_id not in ids


def test_list_clients_filters_by_message_angle(two_clients) -> None:
    first_id, second_id, fund_id = two_clients
    response = client.get(CLIENTS, params={"fund_id": fund_id, "message_angle": "winback_habit"})
    ids = [row["client_id"] for row in response.json()["items"]]
    assert first_id in ids
    assert second_id not in ids


def test_segments_counts_include_the_new_buckets(two_clients) -> None:
    response = client.get(SEGMENTS)
    assert response.status_code == 200
    body = response.json()
    archetypes = {row["key"]: row["count"] for row in body["by_archetype"]}
    assert archetypes.get("One-and-done", 0) >= 1
    assert archetypes.get("Occasional (2-4)", 0) >= 1
