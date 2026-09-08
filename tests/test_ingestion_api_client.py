"""Tests for the Cytonn API client: retries, backoff, and the probe.

The async client is held to the same rules as the blocking one.
"""

from __future__ import annotations

import httpx
import pytest

from app.ingestion.api_client import AsyncCytonnClient, CytonnClient, IngestionAPIError


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


def _async_client(handler, *, sleep, max_attempts=5):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="https://api.example.com", transport=transport)
    return AsyncCytonnClient(
        "https://api.example.com", "secret", client=http, sleep=sleep, max_attempts=max_attempts
    )


async def _no_sleep(seconds: float) -> None:
    return None


def test_async_client_sets_api_key_header():
    client = AsyncCytonnClient("https://api.example.com", "secret")
    assert client._client.headers.get("X-API-Key") == "secret"


async def test_async_retries_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": []})

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    client = _async_client(handler, sleep=sleep)
    assert await client.fetch("/inactive") == {"data": []}
    assert calls["n"] == 3
    assert len(slept) == 2


async def test_async_raises_after_exhausting_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    client = _async_client(handler, sleep=_no_sleep, max_attempts=3)
    with pytest.raises(IngestionAPIError):
        await client.fetch("/x")
    assert calls["n"] == 3


async def test_async_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    client = _async_client(handler, sleep=_no_sleep)
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch("/missing")
    assert calls["n"] == 1


async def test_async_fetch_keeps_a_query_already_on_the_url():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    client = _async_client(handler, sleep=_no_sleep)
    await client.fetch("https://api.example.com/clients?status=active", params={"page": 2})
    assert "status=active" in seen[0]
    assert "page=2" in seen[0]


async def test_async_probe_true_and_false():
    live = _async_client(lambda r: httpx.Response(200), sleep=_no_sleep)
    assert await live.probe() is True

    down = _async_client(lambda r: httpx.Response(500), sleep=_no_sleep)
    assert await down.probe() is False


async def test_async_probe_false_on_transport_error():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    client = _async_client(handler, sleep=_no_sleep)
    assert await client.probe() is False
