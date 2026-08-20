"""The one JSON error shape every endpoint returns: {"error": {code, message, detail}}.

A 4xx's code and message say what to fix; detail carries structured extras
like field-level validation errors when there are any, null otherwise. A 5xx
stays opaque, logged server side but never described to the caller, so an
internal failure never leaks implementation detail.

code normally comes from the status alone, so two different 404s look the
same to a caller. An endpoint that needs them told apart raises ApiError
with its own code rather than making the caller read the message string.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)

_CODES_BY_STATUS = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
}


class ApiError(HTTPException):
    """An HTTPException that names the envelope code itself.

    Use it where one status covers two situations a caller must handle
    differently. Everything else keeps raising a plain HTTPException and
    keeps the code the status maps to.
    """

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def _envelope(code: str, message: str, detail: object | None = None) -> dict:
    return {"error": {"code": code, "message": message, "detail": detail}}


def register_error_handlers(app: FastAPI) -> None:
    """Install the shared exception handlers. Called once from create_app()."""

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", None) or _CODES_BY_STATUS.get(exc.status_code, "http_error")
        return JSONResponse(status_code=exc.status_code, content=_envelope(code, str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_envelope(
                "validation_error", "request validation failed", jsonable_encoder(exc.errors())
            ),
        )

    @app.exception_handler(Exception)
    async def handle_uncaught_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", "an unexpected error occurred"),
        )
