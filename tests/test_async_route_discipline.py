"""An async def endpoint must never reach the database through a blocking session.

Mixing the two stops the event loop for every other request for as long as
the database call takes. The audit walks every route on the real app, and the
deliberately broken app below proves it still catches a breach.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.route_audit import _iter_api_routes, find_blocking_async_routes
from app.config import Settings
from app.db.session import get_session, safe_session
from app.main import app as real_app


def test_the_walk_reaches_the_real_apps_routes() -> None:
    paths = [path for path, _ in _iter_api_routes(real_app.routes)]
    assert "/health" in paths
    assert len(paths) > 50


def test_no_async_endpoint_uses_the_blocking_session() -> None:
    breaches = find_blocking_async_routes(real_app)
    assert breaches == [], "\n".join(b.describe() for b in breaches)


def test_audit_catches_an_async_endpoint_that_depends_on_the_session() -> None:
    broken = FastAPI()
    router = APIRouter()

    @router.get("/broken")
    async def broken_endpoint(session: Session = Depends(get_session)) -> dict[str, str]:
        return {"ok": "no"}

    broken.include_router(router)

    breaches = find_blocking_async_routes(broken)
    assert [b.path for b in breaches] == ["/broken"]
    assert "get_session" in breaches[0].reason


def test_audit_catches_an_async_endpoint_that_opens_a_session_inline() -> None:
    broken = FastAPI()
    router = APIRouter()

    @router.get("/inline")
    async def inline_endpoint() -> dict[str, str]:
        with safe_session():
            return {"ok": "no"}

    broken.include_router(router)

    breaches = find_blocking_async_routes(broken)
    assert [b.path for b in breaches] == ["/inline"]
    assert "safe_session" in breaches[0].reason


def test_audit_accepts_a_plain_def_endpoint_using_the_session() -> None:
    fine = FastAPI()
    router = APIRouter()

    @router.get("/fine")
    def fine_endpoint(session: Session = Depends(get_session)) -> dict[str, str]:
        return {"ok": "yes"}

    fine.include_router(router)

    assert find_blocking_async_routes(fine) == []


def test_worker_threads_are_never_fewer_than_the_database_pool() -> None:
    generous = Settings(worker_threads=40, db_pool_size=10, db_max_overflow=10)
    assert generous.worker_thread_count == 40

    stingy = Settings(worker_threads=4, db_pool_size=30, db_max_overflow=20)
    assert stingy.worker_thread_count == 50
    assert stingy.worker_thread_count >= stingy.max_db_connections


def test_a_slow_endpoint_does_not_stall_the_health_check() -> None:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 20
        yield

    app = FastAPI(lifespan=lifespan)
    router = APIRouter()
    release = threading.Event()

    @router.get("/slow")
    def slow_endpoint() -> dict[str, str]:
        release.wait(timeout=10)
        return {"status": "slow"}

    @router.get("/quick")
    def quick_endpoint() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)

    with TestClient(app) as client:
        slow_calls = [threading.Thread(target=lambda: client.get("/slow")) for _ in range(8)]
        for call in slow_calls:
            call.start()
        try:
            time.sleep(0.2)
            started = time.perf_counter()
            response = client.get("/quick")
            elapsed = time.perf_counter() - started
        finally:
            release.set()
            for call in slow_calls:
                call.join(timeout=10)

    assert response.status_code == 200
    assert elapsed < 2.0


def test_the_response_carries_a_duration_header() -> None:
    client = TestClient(real_app)
    response = client.get("/health")
    assert float(response.headers["X-Response-Time-Ms"]) >= 0
