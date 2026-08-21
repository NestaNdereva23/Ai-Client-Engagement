"""Shared test fixtures.

Worker tests need PostgreSQL (JSONB and upsert). When no database is reachable
they are skipped rather than failed, so the rest of the suite still runs.

Tests never run against the database configured for real data: DATABASE_URL
is redirected here, before anything imports app.db.session, to a dedicated
database (the same name with _test appended, or TEST_DATABASE_URL if set
explicitly). Fixtures pick small, low-numbered ids that would otherwise
collide with real client/fund ids from an actual Cytonn pull; on a shared
database an upsert-based transform silently overwrites real rows with test
data. Create the database once (`CREATE DATABASE ace_test`) and apply
migrations to it before running the suite for the first time.
"""

from __future__ import annotations

import os
import re

import jwt
import pytest
from sqlalchemy import text

from app.config import get_settings


def _redirect_to_test_database() -> None:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        os.environ["DATABASE_URL"] = explicit
    else:
        base = get_settings().database_url
        # .../ace -> .../ace_test ; leaves a query string, if any, untouched.
        os.environ["DATABASE_URL"] = re.sub(
            r"/([^/?]+)(\?.*)?$", lambda m: f"/{m.group(1)}_test{m.group(2) or ''}", base
        )
    get_settings.cache_clear()


_redirect_to_test_database()

import app.db.models  # noqa: E402,F401  (registers models on Base.metadata)
from app.db.base import Base  # noqa: E402
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


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables() -> None:
    """Create tables if they are missing, so worker tests can run locally."""
    if DB_AVAILABLE:
        Base.metadata.create_all(engine)


# A small fixed pair of reviewer identities shared by every test that needs
# the Authorization: Bearer gate (app.api.reviewer_auth). Two reviewers, so
# tests that check "this action recorded which reviewer did it" have two
# real identities to tell apart. Tokens are signed with a fixed test
# secret, standing in for Ticketing's real, per-user issued JWT.
_TEST_JWT_SECRET = "test-jwt-secret"
REVIEWER_1_ID = "fa-1"
REVIEWER_2_ID = "fa-2"


def _make_token(email: str) -> str:
    return jwt.encode({"email": email}, _TEST_JWT_SECRET, algorithm="HS256")


REVIEWER_1_HEADERS = {"Authorization": f"Bearer {_make_token(REVIEWER_1_ID)}"}
REVIEWER_2_HEADERS = {"Authorization": f"Bearer {_make_token(REVIEWER_2_ID)}"}


class _ConfiguredReviewersSettings:
    ai_outreach_jwt_secret = _TEST_JWT_SECRET


class _UnconfiguredReviewersSettings:
    ai_outreach_jwt_secret = ""


@pytest.fixture
def reviewer_1_headers() -> dict[str, str]:
    """Authorization: Bearer header for the first fixed test reviewer.

    A fixture, not a plain import, so test modules never need
    `from conftest import ...` -- that bare module name collides with the
    unrelated conftest.py under scripts/ once the whole suite runs together.
    """
    return REVIEWER_1_HEADERS


@pytest.fixture
def reviewer_2_headers() -> dict[str, str]:
    """Authorization: Bearer header for the second fixed test reviewer."""
    return REVIEWER_2_HEADERS


@pytest.fixture
def configured_reviewers(monkeypatch):
    """Point app.api.reviewer_auth at the fixed test JWT secret above."""
    from app.api import reviewer_auth

    monkeypatch.setattr(reviewer_auth, "get_settings", lambda: _ConfiguredReviewersSettings())


@pytest.fixture
def unconfigured_reviewers(monkeypatch):
    """Point app.api.reviewer_auth at no secret at all, for the 503 case."""
    from app.api import reviewer_auth

    monkeypatch.setattr(reviewer_auth, "get_settings", lambda: _UnconfiguredReviewersSettings())


@pytest.fixture
def cleanup_runs():
    """Collect run ids to delete after the test, keeping the database clean."""
    run_ids: list[str] = []
    yield run_ids
    if not DB_AVAILABLE:
        return
    with SessionLocal() as session:
        for run_id in run_ids:
            session.execute(text("DELETE FROM ingestion_rejects WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM raw_staging WHERE run_id = :r"), {"r": run_id})
            session.execute(text("DELETE FROM ingestion_status WHERE run_id = :r"), {"r": run_id})
        session.commit()
