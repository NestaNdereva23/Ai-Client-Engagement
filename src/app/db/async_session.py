"""Async database engine and session lifecycle, alongside the blocking one.

Two separate pools on the same database. Each has its own cap, so neither
path can take every connection and leave the other waiting.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import _settings, timezone_connect_args


def use_selector_event_loop_on_windows() -> None:
    """Psycopg cannot talk to Postgres on the proactor loop Windows picks by
    default, so ask for the selector loop instead. A no-op everywhere else.
    """
    if sys.platform != "win32":
        return
    policy = asyncio.WindowsSelectorEventLoopPolicy()
    if not isinstance(asyncio.get_event_loop_policy(), type(policy)):
        asyncio.set_event_loop_policy(policy)


use_selector_event_loop_on_windows()


async_engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=_settings.async_pool_size,
    max_overflow=_settings.async_max_overflow,
    connect_args=timezone_connect_args(_settings.db_timezone),
    future=True,
)


AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_async_session() -> AsyncIterator[AsyncSession]:
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()


@asynccontextmanager
async def async_safe_session() -> AsyncIterator[AsyncSession]:
    """Model-facing async session under the safe role, which cannot read pii_vault.

    Bound to one explicit connection for its whole lifetime, for the same
    reason the blocking version is: the role must be reset on the connection
    it was set on, before that connection goes back to the pool.
    """
    connection = await async_engine.connect()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        await session.execute(text(f'SET ROLE "{_settings.db_safe_role}"'))
        yield session
    finally:
        await session.rollback()
        await session.execute(text("RESET ROLE"))
        await session.commit()
        await session.close()
        await connection.close()


@asynccontextmanager
async def async_restricted_session() -> AsyncIterator[AsyncSession]:
    """Vault-facing async session under the restricted role, the only one with
    a grant on pii_vault.

    Bound to one explicit connection for its whole lifetime, for the same
    reason the blocking version is: the role must be reset on the connection
    it was set on, before that connection goes back to the pool.
    """
    connection = await async_engine.connect()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        await session.execute(text(f'SET ROLE "{_settings.db_restricted_role}"'))
        yield session
    finally:
        await session.rollback()
        await session.execute(text("RESET ROLE"))
        await session.commit()
        await session.close()
        await connection.close()


async def check_async_connection() -> bool:
    async with async_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def dispose_async_engine() -> None:
    await async_engine.dispose()
