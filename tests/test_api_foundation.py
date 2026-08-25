"""The shared API foundation: versioning, the error envelope, request-id, and
pagination bounds. test_api_review.py already exercises most of this through
a real business endpoint; these target the pieces that endpoint can't reach
on its own, an unknown route, a bad cursor, and a genuinely uncaught 500.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import register_error_handlers
from app.main import app as real_app

client = TestClient(real_app)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_health_stays_unversioned() -> None:
    response = client.get("/health")
    assert response.status_code in (200, 503)


def test_an_unknown_route_under_v1_returns_the_error_envelope() -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]


def test_request_id_is_echoed_back_when_supplied() -> None:
    response = client.get("/health", headers={"X-Request-ID": "test-request-id"})
    assert response.headers["X-Request-ID"] == "test-request-id"


def test_request_id_is_generated_when_absent() -> None:
    response = client.get("/health")
    assert response.headers["X-Request-ID"]


def test_an_out_of_range_limit_is_a_validation_error() -> None:
    response = client.get("/api/v1/reviews", params={"limit": 0})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["detail"], list)


def test_an_invalid_cursor_is_a_bad_request() -> None:
    response = client.get("/api/v1/reviews", params={"cursor": "not-a-real-cursor"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_an_uncaught_exception_returns_the_opaque_500_envelope() -> None:
    isolated = FastAPI()
    register_error_handlers(isolated)

    @isolated.get("/boom")
    def boom() -> None:
        raise RuntimeError("internal detail that must never reach the caller")

    # Starlette re-raises past a caught 500 for visibility; the real client
    # still gets the clean response, but the test needs to see it too.
    with TestClient(isolated, raise_server_exceptions=False) as isolated_client:
        response = isolated_client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "RuntimeError" not in body["error"]["message"]
    assert "internal detail" not in body["error"]["message"]
