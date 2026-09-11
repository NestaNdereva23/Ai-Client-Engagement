"""HTTP client for the Cytonn inactive clients endpoint.

Sends the API key as the X-API-Key header on every call, applies a request
timeout, and retries temporary failures (timeouts, connection errors, 429 and
5xx) with exponential backoff and jitter. Other client errors (4xx) are not
retried. A small probe checks the endpoint is reachable before a full run.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Status codes worth retrying: rate limiting and server errors.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class IngestionAPIError(RuntimeError):
    """Raised when a request ultimately fails after exhausting retries."""


def backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """Random wait between zero and base times two to the attempt, capped."""
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)


def request_url(base_url: str, path: str, params: dict[str, Any] | None) -> httpx.URL:
    """The url one fetch asks for.

    Merges params into the url ourselves: some endpoint urls already carry a
    query string (for example a "status=active" filter), and passing params
    straight to httpx replaces that query instead of adding to it, silently
    dropping the filter.
    """
    url = httpx.URL(path or base_url)
    if params:
        url = url.copy_merge_params(params)
    return url


def retryable_error(response: httpx.Response) -> httpx.HTTPStatusError | None:
    """The error to retry on for this response, or None if it is worth reading.

    A response that is neither retryable nor successful raises here, the same
    way it does on the blocking path and the async one alike.
    """
    if response.status_code not in RETRYABLE_STATUS:
        response.raise_for_status()
        return None
    return httpx.HTTPStatusError(
        f"retryable status {response.status_code}",
        request=response.request,
        response=response,
    )


def probe_result(response: httpx.Response) -> bool:
    """Whether one probe response counts as live."""
    live = response.status_code < 500
    if not live:
        logger.warning("ingestion.probe.server_error", status_code=response.status_code)
    return live


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
        return backoff_delay(attempt, base=self._backoff_base, cap=self._backoff_cap)

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
        url = request_url(self._base_url, path, params)

        for attempt in range(self._max_attempts):
            try:
                response = self._client.get(url)
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning(
                    "ingestion.fetch.transport_error",
                    attempt=attempt + 1,
                    error=str(exc),
                )
            else:
                error = retryable_error(response)
                if error is None:
                    return response.json()
                last_error = error
                logger.warning(
                    "ingestion.fetch.retryable_status",
                    attempt=attempt + 1,
                    status_code=response.status_code,
                )

            # Do not sleep after the final attempt.
            if attempt < self._max_attempts - 1:
                self._sleep(self._backoff_delay(attempt))

        raise IngestionAPIError(
            f"request to {url!r} failed after {self._max_attempts} attempts"
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
        return probe_result(response)


class AsyncCytonnClient:
    """The async twin of CytonnClient, for the long pulls.

    Same headers, same retry rule, same backoff and the same errors: it waits
    on the network instead of holding a thread while a slow page comes back.
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
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    async def __aenter__(self) -> AsyncCytonnClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    def _backoff_delay(self, attempt: int) -> float:
        """Random wait between zero and base times two to the attempt, capped."""
        return backoff_delay(attempt, base=self._backoff_base, cap=self._backoff_cap)

    async def fetch(
        self,
        path: str = "",
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get the path and return the parsed JSON body, awaiting the network."""
        last_error: Exception | None = None
        url = request_url(self._base_url, path, params)

        for attempt in range(self._max_attempts):
            try:
                response = await self._client.get(url)
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning(
                    "ingestion.fetch.transport_error",
                    attempt=attempt + 1,
                    error=str(exc),
                )
            else:
                error = retryable_error(response)
                if error is None:
                    return response.json()
                last_error = error
                logger.warning(
                    "ingestion.fetch.retryable_status",
                    attempt=attempt + 1,
                    status_code=response.status_code,
                )

            # Do not sleep after the final attempt.
            if attempt < self._max_attempts - 1:
                await self._sleep(self._backoff_delay(attempt))

        raise IngestionAPIError(
            f"request to {url!r} failed after {self._max_attempts} attempts"
        ) from last_error

    async def probe(self, path: str = "") -> bool:
        """Return True if the endpoint is reachable and not returning a server error."""
        try:
            response = await self._client.get(path or self._base_url)
        except httpx.TransportError as exc:
            logger.warning("ingestion.probe.unreachable", error=str(exc))
            return False
        return probe_result(response)
