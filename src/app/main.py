"""FastAPI application factory: builds and configures the web application."""

from __future__ import annotations

from fastapi import FastAPI

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

    app.add_middleware(CorrelationIdMiddleware)

    # Routers
    app.include_router(health.router)

    return app


app = create_app()
