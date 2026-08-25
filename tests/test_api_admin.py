from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FUNNEL = "/api/v1/admin/metrics/funnel"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_missing_token_is_401(configured_reviewers) -> None:
    response = TestClient(app).get(FUNNEL)
    assert response.status_code == 401


def test_no_reviewer_configured_is_503(unconfigured_reviewers, reviewer_1_headers) -> None:
    response = TestClient(app).get(FUNNEL, headers=reviewer_1_headers)
    assert response.status_code == 503


def test_funnel_counts_with_a_valid_token(db) -> None:
    response = client.get(FUNNEL)
    assert response.status_code == 200
    assert "generated" in response.json()
