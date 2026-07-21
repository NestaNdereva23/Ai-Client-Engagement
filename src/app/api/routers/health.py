"""Health check endpoint: application liveness and a database ping."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from app.db.session import check_connection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health(response: Response) -> dict[str, object]:
    """Report application liveness and database connectivity.

    Returns 200 when the app is up and the database answers, and 503 when the
    database cannot be reached (the app itself is still running).
    """
    database_ok = True
    try:
        check_connection()
    except Exception:
        database_ok = False
        logger.exception("Health check: database ping failed")

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if database_ok else "degraded",
        "checks": {
            "app": "ok",
            "database": "ok" if database_ok else "error",
        },
    }
