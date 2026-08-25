from __future__ import annotations

from app.config import Settings
from app.main import create_app


def test_docs_disabled_in_production() -> None:
    app = create_app(Settings(app_env="production"))
    assert app.docs_url is None
    assert app.openapi_url is None


def test_docs_enabled_outside_production() -> None:
    app = create_app(Settings(app_env="development"))
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"
