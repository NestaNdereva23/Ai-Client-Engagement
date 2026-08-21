"""FastAPI application factory: builds and configures the web application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqladmin import Admin
from starlette.middleware.sessions import SessionMiddleware

from app.admin.auth import AdminAuth
from app.admin.views import ADMIN_VIEWS
from app.api import v1
from app.api.errors import register_error_handlers
from app.api.idempotency import IdempotencyMiddleware
from app.api.middleware import CorrelationIdMiddleware
from app.api.routers import health, reviewer_ui
from app.config import Settings, get_settings
from app.db.session import engine
from app.llmops.tracing import shutdown_shared_tracer
from app.logging_config import configure_logging

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release the shared tracer's background export thread on shutdown.

    Nothing to set up: the tracer is built on the first request that needs
    one, and a deployment with Langfuse unconfigured never builds one at all.
    """
    yield
    shutdown_shared_tracer()


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Build and configure the FastAPI application.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.is_production)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings

    register_error_handlers(app)
    # Added first, which Starlette wraps outermost -- so CORS headers still
    # land on a response either of the next two middlewares errors out on.
    # allow_origins from settings, not "*": IdempotencyMiddleware replays a
    # stored response by Idempotency-Key, and a wildcard origin can't be
    # combined with allow_credentials if that's ever turned on later.
    if settings.cors_allow_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins_list,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    # Backs the reviewer console's login (app.auth.session). Isolated from
    # sqladmin's own session below: sqladmin mounts a separate Starlette
    # sub-app with its own middleware stack, so the two never share state.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.console_session_secret_key,
        session_cookie="ace_session",
        same_site="lax",
        https_only=settings.is_production,
    )

    # Routers
    app.include_router(health.router)
    app.include_router(v1.router)
    app.include_router(reviewer_ui.router)

    admin = Admin(
        app, engine, authentication_backend=AdminAuth(secret_key=settings.admin_secret_key)
    )
    for view in ADMIN_VIEWS:
        admin.add_view(view)

    return app


app = create_app()
