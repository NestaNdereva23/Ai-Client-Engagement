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

_DB_PATH_RE = re.compile(r"/([^/?]+)(\?.*)?$")


def _redirect_to_test_database() -> None:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        base = explicit
    else:
        # Idempotent: the xdist controller process runs this same function
        # and leaks its already-suffixed DATABASE_URL to worker processes
        # through the inherited environment, so a worker must not suffix twice.
        base = get_settings().database_url
        name = _DB_PATH_RE.search(base).group(1)
        if not name.endswith("_test"):
            base = _DB_PATH_RE.sub(lambda m: f"/{name}_test{m.group(2) or ''}", base)

    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id:
        base = _clone_worker_database(base, worker_id)

    os.environ["DATABASE_URL"] = base
    get_settings.cache_clear()


def _clone_worker_database(template_url: str, worker_id: str) -> str:
    """Point this xdist worker at its own copy of the test database.

    Mirrors tests/conftest.py's version of the same helper.
    """
    from sqlalchemy import create_engine

    template_db = _DB_PATH_RE.search(template_url).group(1)
    worker_db = f"{template_db}_{worker_id}"
    worker_url = _DB_PATH_RE.sub(lambda m: f"/{worker_db}{m.group(2) or ''}", template_url)
    admin_url = _DB_PATH_RE.sub(lambda m: f"/postgres{m.group(2) or ''}", template_url)

    try:
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{worker_db}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{worker_db}" TEMPLATE "{template_db}"'))
        engine.dispose()
    except Exception:
        pass  # unreachable database; _db_available() below reports it and tests skip

    return worker_url


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
# An idle pooled connection to this database blocks CREATE DATABASE ... TEMPLATE
# (used to clone per-worker test databases under xdist), so don't leave one open
# in the controller process, which never runs a test but stays up for the whole run.
engine.dispose()


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
