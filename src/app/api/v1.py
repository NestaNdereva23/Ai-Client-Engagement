"""The versioned API surface: every domain router mounts here.

A breaking change to a mounted router's contract bumps this prefix to v2
rather than changing v1 out from under an existing caller. health stays
outside this surface; it is an infra liveness probe, not part of the
console/integration contract in design §9A.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import review

router = APIRouter(prefix="/api/v1")
router.include_router(review.router)
