from __future__ import annotations

import logging

from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.logging_config import _HideHealthChecks
from app.main import app

client = TestClient(app)


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 200),
        None,
    )


def test_a_passing_health_check_writes_no_request_log() -> None:
    with capture_logs() as logs:
        client.get("/health")

    assert not [entry for entry in logs if entry.get("path") == "/health"]


def test_other_requests_are_still_logged() -> None:
    with capture_logs() as logs:
        client.get("/openapi.json")

    assert [entry for entry in logs if entry.get("path") == "/openapi.json"]


def test_uvicorn_access_line_for_health_is_dropped() -> None:
    assert _HideHealthChecks().filter(_access_record("/health")) is False


def test_uvicorn_access_line_for_other_paths_is_kept() -> None:
    assert _HideHealthChecks().filter(_access_record("/campaigns")) is True
