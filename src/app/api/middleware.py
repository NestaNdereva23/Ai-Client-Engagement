"""HTTP middleware."""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.api.idempotency import call_next_shielded

REQUEST_ID_HEADER = "X-Request-ID"


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
        try:
            response = await call_next_shielded(call_next, request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
