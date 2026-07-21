"""FastAPI application factory: builds and configures the web application."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.routers import health
from app.config import Settings, get_settings

__version__ = "0.1.0"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """
        Build and configure the FastAPI application.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # Routers
    app.include_router(health.router)

    return app


app = create_app()
