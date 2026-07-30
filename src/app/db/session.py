"""Database engine and session lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)


@event.listens_for(engine, "connect")
def _set_session_timezone(dbapi_connection, _connection_record):
    """Make each connection report timestamps in the configured time zone."""
    with dbapi_connection.cursor() as cursor:
        cursor.execute(f"SET TIME ZONE '{_settings.db_timezone}'")


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def safe_session() -> Iterator[Session]:
    """Model-facing session running under the safe role, which cannot read pii_vault.

    Bound to one explicit connection for its whole lifetime: a commit inside
    the block must not check the connection back into the pool while the role
    is still set on it, or the reset below could land on a different
    connection and leak the role into whatever picks that one up next. The
    role is reset on that same connection before it returns to the pool.
    """
    connection = engine.connect()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        session.execute(text(f'SET ROLE "{_settings.db_safe_role}"'))
        yield session
    finally:
        session.rollback()
        session.execute(text("RESET ROLE"))
        session.commit()
        session.close()
        connection.close()


@contextmanager
def restricted_session() -> Iterator[Session]:
    """Vault-facing session running under the restricted role, the only one
    with a grant on pii_vault.

    Bound to one explicit connection for its whole lifetime: a commit inside
    the block must not check the connection back into the pool while the role
    is still set on it, or the reset below could land on a different
    connection and leak the role into whatever picks that one up next. The
    role is reset on that same connection before it returns to the pool.
    """
    connection = engine.connect()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        session.execute(text(f'SET ROLE "{_settings.db_restricted_role}"'))
        yield session
    finally:
        session.rollback()
        session.execute(text("RESET ROLE"))
        session.commit()
        session.close()
        connection.close()


def check_connection() -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
