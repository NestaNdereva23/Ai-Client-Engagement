"""HTTP client for the Cytonn inactive clients endpoint.

Sends the API key as the X-API-Key header on every call, applies a request
timeout, and retries temporary failures (timeouts, connection errors, 429 and
5xx) with exponential backoff and jitter. Other client errors (4xx) are not
retried. A small probe checks the endpoint is reachable before a full run.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Status codes worth retrying: rate limiting and server errors.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class IngestionAPIError(RuntimeError):
    """Raised when a request ultimately fails after exhausting retries."""


class CytonnClient:
    """Client for the inactive clients endpoint with retries and a liveness probe.

    Pass in an httpx client and a sleep function to make tests fast and offline.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        max_attempts: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    def __enter__(self) -> CytonnClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying httpx client if this instance created it."""
        if self._owns_client:
            self._client.close()

    def _backoff_delay(self, attempt: int) -> float:
        """Random wait between zero and base times two to the attempt, capped."""
        ceiling = min(self._backoff_cap, self._backoff_base * (2**attempt))
        return random.uniform(0, ceiling)

    def fetch(
        self,
        path: str = "",
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get the path and return the parsed JSON body.

        Retries temporary failures with backoff. Raises IngestionAPIError once
        the retries run out, or right away on a client error we do not retry.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            try:
                response = self._client.get(path or self._base_url, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning(
                    "ingestion.fetch.transport_error",
                    attempt=attempt + 1,
                    error=str(exc),
                )
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response.json()
                last_error = httpx.HTTPStatusError(
                    f"retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
                logger.warning(
                    "ingestion.fetch.retryable_status",
                    attempt=attempt + 1,
                    status_code=response.status_code,
                )

            # Do not sleep after the final attempt.
            if attempt < self._max_attempts - 1:
                self._sleep(self._backoff_delay(attempt))

        raise IngestionAPIError(
            f"request to {path or self._base_url!r} failed after {self._max_attempts} attempts"
        ) from last_error

    def probe(self, path: str = "") -> bool:
        """Return True if the endpoint is reachable and not returning a server error.

        One request, no retries. Any transport error or 5xx counts as not live.
        Used as a quick check before a full run.
        """
        try:
            response = self._client.get(path or self._base_url)
        except httpx.TransportError as exc:
            logger.warning("ingestion.probe.unreachable", error=str(exc))
            return False
        live = response.status_code < 500
        if not live:
            logger.warning("ingestion.probe.server_error", status_code=response.status_code)
        return live
