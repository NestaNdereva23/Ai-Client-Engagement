"""The async database path must behave exactly like the blocking one.

Same database, same time zone, its own pool and its own transaction. Nothing
here changes the blocking path; it only proves the two agree.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db.async_session import (
    AsyncSessionLocal,
    async_engine,
    check_async_connection,
    dispose_async_engine,
    get_async_session,
)
from app.db.models.agent_run import AgentRun, AgentToolCall
from app.db.session import SessionLocal, engine
from app.services.agent_runs import (
    get_agent_run,
    get_agent_run_async,
    list_agent_runs_async,
    list_run_tool_calls_async,
)


@pytest.fixture(autouse=True)
async def _dispose_async_engine_after_each_test():
    yield
    await dispose_async_engine()


def _delete_runs(run_ids: list[int]) -> None:
    with SessionLocal() as session:
        session.execute(
            text("DELETE FROM agent_tool_call WHERE run_id = ANY(:ids)"), {"ids": run_ids}
        )
        session.execute(text("DELETE FROM agent_run WHERE run_id = ANY(:ids)"), {"ids": run_ids})
        session.commit()


async def test_the_async_path_reaches_the_same_database():
    assert await check_async_connection() is True


async def test_a_row_written_async_is_read_back_by_the_blocking_path():
    async with AsyncSessionLocal() as session:
        run = AgentRun(trigger="manual", state="running")
        session.add(run)
        await session.commit()
        run_id = run.run_id
    try:
        with SessionLocal() as blocking:
            assert get_agent_run(blocking, run_id).trigger == "manual"
    finally:
        _delete_runs([run_id])


async def test_a_row_written_blocking_is_read_back_by_the_async_path():
    with SessionLocal() as session:
        run = AgentRun(trigger="nightly", state="running")
        session.add(run)
        session.commit()
        run_id = run.run_id
    try:
        async with AsyncSessionLocal() as session:
            assert (await get_agent_run_async(session, run_id)).trigger == "nightly"
            rows, _ = await list_agent_runs_async(session, limit=5)
            assert run_id in [row.run_id for row in rows]
    finally:
        _delete_runs([run_id])


async def test_tool_calls_read_async_come_back_in_order():
    with SessionLocal() as session:
        run = AgentRun(trigger="manual", state="running")
        session.add(run)
        session.commit()
        run_id = run.run_id
        for ordinal in (2, 0, 1):
            session.add(
                AgentToolCall(
                    run_id=run_id,
                    ordinal=ordinal,
                    tool_name=f"tool_{ordinal}",
                    tool_input={},
                    tool_output={},
                )
            )
        session.commit()
    try:
        async with AsyncSessionLocal() as session:
            calls = await list_run_tool_calls_async(session, run_id)
        assert [call.ordinal for call in calls] == [0, 1, 2]
    finally:
        _delete_runs([run_id])


async def test_both_paths_stamp_the_same_time_zone():
    with SessionLocal() as session:
        blocking_zone = session.execute(text("SHOW TIME ZONE")).scalar_one()
    async with AsyncSessionLocal() as session:
        async_zone = (await session.execute(text("SHOW TIME ZONE"))).scalar_one()
    assert async_zone == blocking_zone


async def test_a_rollback_on_one_path_leaves_the_other_alone():
    with SessionLocal() as session:
        run = AgentRun(trigger="manual", state="running")
        session.add(run)
        session.commit()
        run_id = run.run_id
    try:
        async with AsyncSessionLocal() as session:
            doomed = await session.get(AgentRun, run_id)
            doomed.summary = "rolled back"
            await session.flush()
            await session.rollback()
        with SessionLocal() as blocking:
            assert get_agent_run(blocking, run_id).summary is None
    finally:
        _delete_runs([run_id])


async def test_the_async_dependency_yields_a_usable_session():
    generator = get_async_session()
    session = await anext(generator)
    try:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        with pytest.raises(StopAsyncIteration):
            await anext(generator)


async def test_more_async_readers_than_the_pool_all_finish():
    settings = Settings()
    concurrent = settings.max_async_db_connections + 5

    async def read_one() -> int:
        async with AsyncSessionLocal() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    results = await asyncio.gather(*(read_one() for _ in range(concurrent)))
    assert results == [1] * concurrent


def test_the_two_pools_are_capped_separately():
    settings = Settings(db_pool_size=4, db_max_overflow=2)
    assert settings.max_db_connections == 6
    assert settings.max_async_db_connections == 6
    assert settings.max_total_db_connections == 12

    tuned = Settings(db_pool_size=4, db_max_overflow=2, db_async_pool_size=9)
    assert tuned.max_db_connections == 6
    assert tuned.max_async_db_connections == 11


def test_the_blocking_pool_is_not_shared_with_the_async_one():
    assert async_engine.sync_engine is not engine
    assert async_engine.pool is not engine.pool


def test_the_blocking_path_keeps_its_time_zone_across_pool_checkouts():
    zones = []
    for _ in range(3):
        with SessionLocal() as session:
            zones.append(session.execute(text("SHOW TIME ZONE")).scalar_one())
    assert len(set(zones)) == 1
    assert zones[0] == Settings().db_timezone
