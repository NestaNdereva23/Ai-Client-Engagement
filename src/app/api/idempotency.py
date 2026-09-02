"""Idempotency-Key handling for state-changing requests.

A POST/PUT/PATCH/DELETE carrying an Idempotency-Key header is looked up by
(key, method, path) before it runs; a match returns the first response
instead of re-executing the handler, so a client's retry after a dropped
connection or a timeout can never double-apply a write. The lookup and the
store both run off the event loop, in a thread pool, since they use the same
synchronous SQLAlchemy session as the rest of the app.

A concurrent request racing on the same key can still both execute; the
store step is best effort and swallows the resulting duplicate-key conflict,
since the response already computed and returned to each caller is correct
either way, only the cache for a future replay is what's lost.

call_next() runs the route handler (and everything it does, including a
database session held open for the whole call) as a task tied to this
request. If the client disconnects before that finishes, Starlette cancels
the task -- which tears down the handler's session mid-work instead of
letting it finish and commit. A long write (drafting several templates,
say) can lose everything from that call, not just the final response, and
the caller pays for LLM calls whose results never reach the database.
Shielding the call keeps a dropped connection from reaching past this
middleware into the handler; the handler still finishes and commits, only
the reply back to the now-gone client is wasted.
"""

from __future__ import annotations

import json

import anyio
import structlog
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.db.models.api import IdempotencyKey
from app.db.session import SessionLocal

IDEMPOTENCY_HEADER = "Idempotency-Key"
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

logger = structlog.get_logger(__name__)


async def call_next_shielded(call_next: RequestResponseEndpoint, request: Request) -> Response:
    """call_next(), immune to the client disconnecting before it returns.

    A shielded scope still runs to completion normally; it only refuses a
    cancellation arriving from outside itself, which is exactly what a
    dropped client connection delivers.
    """
    with anyio.CancelScope(shield=True):
        return await call_next(request)


def _find_cached(key: str, method: str, path: str) -> IdempotencyKey | None:
    with SessionLocal() as session:
        return session.get(IdempotencyKey, (key, method, path))


def _store(key: str, method: str, path: str, status_code: int, body: bytes) -> None:
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return
    with SessionLocal() as session:
        session.add(
            IdempotencyKey(
                idempotency_key=key,
                method=method,
                path=path,
                status_code=status_code,
                response_body=parsed,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.debug("idempotency_key_race", key=key, method=method, path=path)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Replay the stored response for a repeated Idempotency-Key."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method not in _STATE_CHANGING_METHODS or not key:
            return await call_next_shielded(call_next, request)

        path = request.url.path
        cached = await run_in_threadpool(_find_cached, key, request.method, path)
        if cached is not None:
            return JSONResponse(status_code=cached.status_code, content=cached.response_body)

        response = await call_next_shielded(call_next, request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        if response.status_code < 500:
            await run_in_threadpool(_store, key, request.method, path, response.status_code, body)

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
