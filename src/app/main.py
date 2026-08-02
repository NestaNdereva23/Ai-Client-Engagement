"""FastAPI application factory: builds and configures the web application."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import v1
from app.api.errors import register_error_handlers
from app.api.idempotency import IdempotencyMiddleware
from app.api.middleware import CorrelationIdMiddleware
from app.api.routers import health
from app.config import Settings, get_settings
from app.logging_config import configure_logging

__version__ = "0.1.0"


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
    )
    app.state.settings = settings

    register_error_handlers(app)
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # Routers
    app.include_router(health.router)
    app.include_router(v1.router)

    return app


app = create_app()
