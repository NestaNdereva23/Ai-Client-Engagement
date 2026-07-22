"""Tests for the Cytonn API client: retries, backoff, and the probe."""

from __future__ import annotations

import httpx
import pytest

from app.ingestion.api_client import CytonnClient, IngestionAPIError


def _client(handler, *, sleep, max_attempts=5):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://api.example.com", transport=transport)
    return CytonnClient(
        "https://api.example.com", "secret", client=http, sleep=sleep, max_attempts=max_attempts
    )


def test_sets_api_key_header():
    client = CytonnClient("https://api.example.com", "secret")
    assert client._client.headers.get("X-API-Key") == "secret"
    client.close()


def test_retries_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": []})

    client = _client(handler, sleep=slept.append)
    assert client.fetch("/inactive") == {"data": []}
    assert calls["n"] == 3
    assert len(slept) == 2  # no sleep after the successful attempt


def test_raises_after_exhausting_retries():
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    client = _client(handler, sleep=slept.append, max_attempts=3)
    with pytest.raises(IngestionAPIError):
        client.fetch("/x")
    assert calls["n"] == 3
    assert len(slept) == 2


def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    client = _client(handler, sleep=lambda _: None)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch("/missing")
    assert calls["n"] == 1


def test_probe_true_and_false():
    live = _client(lambda r: httpx.Response(200), sleep=lambda _: None)
    assert live.probe() is True

    down = _client(lambda r: httpx.Response(500), sleep=lambda _: None)
    assert down.probe() is False


def test_probe_false_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    client = _client(handler, sleep=lambda _: None)
    assert client.probe() is False
