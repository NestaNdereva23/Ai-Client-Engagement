"""Database engine and session lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()


def timezone_connect_args(timezone: str) -> dict[str, str]:
    """Ask for the time zone as a connection startup option.

    It has to be a startup option rather than a SET statement, because a SET
    runs inside the transaction it lands in and is undone by the rollback the
    pool does when the connection is handed back.
    """
    return {"options": f"-c timezone={timezone}"}


engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    connect_args=timezone_connect_args(_settings.db_timezone),
    future=True,
)


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
