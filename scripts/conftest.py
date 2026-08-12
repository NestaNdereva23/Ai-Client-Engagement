"""Shared fixtures for the script-level regression tests under scripts/*/tests/.

Mirrors tests/conftest.py's database redirect so these tests never touch the
real database. Kept as its own copy rather than imported from tests/conftest.py
so the scripts/ tree does not depend on the app's tests/ package existing.
"""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import text

from app.config import get_settings


def _redirect_to_test_database() -> None:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        os.environ["DATABASE_URL"] = explicit
    else:
        base = get_settings().database_url
        os.environ["DATABASE_URL"] = re.sub(
            r"/([^/?]+)(\?.*)?$", lambda m: f"/{m.group(1)}_test{m.group(2) or ''}", base
        )
    get_settings.cache_clear()


_redirect_to_test_database()

from app.db.session import SessionLocal, engine  # noqa: E402


def _db_available() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture
def db() -> None:
    """Skip a test cleanly when no database is reachable."""
    if not DB_AVAILABLE:
        pytest.skip("database not available")


@pytest.fixture
def cleanup_runs():
    """Collect run ids to delete after the test, keeping the database clean."""
    run_ids: list[str] = []
    yield run_ids
    if not DB_AVAILABLE:
        return
    with SessionLocal() as session:
        for run_id in run_ids:
            session.execute(text("DELETE FROM raw_staging WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM ingestion_status WHERE run_id = :r"), {"r": run_id})
        session.commit()
