"""The PII boundary is enforced by DB roles, not just application code.

These run against a database with the role migration applied. When the roles are
absent (a schema created with create_all only) they skip rather than fail.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db.session import SessionLocal, safe_session

SAFE = "ace_safe"
RESTRICTED = "ace_restricted"


def _role_exists(session, name: str) -> bool:
    return bool(session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}))


@pytest.fixture
def roles(db: None):
    """Skip unless the boundary roles exist, i.e. the migration has been applied."""
    with SessionLocal() as session:
        if not _role_exists(session, SAFE):
            pytest.skip("boundary roles not present; run alembic upgrade head")


def test_boundary_roles_exist_and_cannot_log_in(roles) -> None:
    with SessionLocal() as session:
        rows = session.execute(
            text("SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname IN (:a, :b)"),
            {"a": SAFE, "b": RESTRICTED},
        ).all()
    canlogin = {name: login for name, login in rows}
    assert canlogin == {SAFE: False, RESTRICTED: False}


def test_safe_role_cannot_read_the_vault(roles) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{SAFE}"'))
        with pytest.raises(ProgrammingError, match="permission denied"):
            session.execute(text("SELECT count(*) FROM pii_vault"))
        session.rollback()
        session.execute(text("RESET ROLE"))


def test_safe_role_can_read_model_facing_features(roles) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{SAFE}"'))
        count = session.scalar(text("SELECT count(*) FROM client_features"))
        session.execute(text("RESET ROLE"))
    assert count is not None


def test_restricted_role_can_read_the_vault(roles) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{RESTRICTED}"'))
        count = session.scalar(text("SELECT count(*) FROM pii_vault"))
        session.execute(text("RESET ROLE"))
    assert count is not None


def test_safe_session_helper_denies_the_vault(roles) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        with safe_session() as session:
            session.execute(text("SELECT count(*) FROM pii_vault"))


def test_safe_session_resets_the_role_for_later_callers(roles) -> None:
    # A safe session must leave the pooled connection able to read the vault again.
    with safe_session() as session:
        session.execute(text("SELECT count(*) FROM client_features"))
    with SessionLocal() as session:
        assert session.scalar(text("SELECT count(*) FROM pii_vault")) is not None
