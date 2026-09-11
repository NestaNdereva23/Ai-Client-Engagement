"""HTTP middleware."""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.api.idempotency import call_next_shielded
from app.config import get_settings

REQUEST_ID_HEADER = "X-Request-ID"
DURATION_HEADER = "X-Response-Time-Ms"

_log = structlog.get_logger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Assign a correlation id to each request, bind it for logging, and echo it back.

    call_next() is shielded from cancellation: without that, a client
    disconnecting mid-request cancels the route handler underneath this
    middleware, tearing down whatever it was doing (a database session held
    open for a long write, for instance) instead of letting it finish. See
    idempotency.call_next_shielded for the full reasoning.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next_shielded(call_next, request)
        finally:
            structlog.contextvars.clear_contextvars()
        duration_ms = (time.perf_counter() - started) * 1000
        self._log_duration(request, request_id, response.status_code, duration_ms)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[DURATION_HEADER] = f"{duration_ms:.1f}"
        return response

    @staticmethod
    def _log_duration(
        request: Request, request_id: str, status_code: int, duration_ms: float
    ) -> None:
        route = request.scope.get("route")
        entry = {
            "request_id": request_id,
            "method": request.method,
            "path": getattr(route, "path", request.url.path),
            "status_code": status_code,
            "duration_ms": round(duration_ms, 1),
        }
        if duration_ms >= get_settings().slow_request_ms:
            _log.warning("slow_request", **entry)
        else:
            _log.info("request_finished", **entry)
