"""get_db admission control (pool-deadlock fix).

A 100-user burst under the 0.5 CPU profile deadlocked the connection pool:
requests held connections between worker-thread hops while every worker
thread waited for a connection. get_db now admits at most as many requests as
the pool can serve, waiting on the event loop instead of in a worker thread.
"""
import asyncio

import pytest

from app import database


def test_slot_count_leaves_reserved_connections():
    pool = database.engine.pool
    expected = pool.size() + max(0, pool._max_overflow) - database._RESERVED_CONNECTIONS
    assert database._request_session_slots() == max(1, expected)


def test_second_request_waits_for_a_free_slot(monkeypatch):
    monkeypatch.setattr(database, "_request_session_slots", lambda: 1)
    monkeypatch.setattr(database, "_slots_by_loop", __import__("weakref").WeakKeyDictionary())

    async def scenario():
        first = database.get_db()
        await first.__anext__()                      # holds the only slot
        second = database.get_db()
        waiting = asyncio.ensure_future(second.__anext__())
        await asyncio.sleep(0.05)
        assert not waiting.done(), "second request must wait while the slot is taken"
        with pytest.raises(StopAsyncIteration):
            await first.__anext__()                  # teardown frees the slot
        session = await asyncio.wait_for(waiting, timeout=2)
        assert session is not None
        with pytest.raises(StopAsyncIteration):
            await second.__anext__()

    asyncio.run(scenario())


def test_each_event_loop_gets_its_own_semaphore():
    async def grab():
        return database._session_slots()

    first, second = asyncio.run(grab()), asyncio.run(grab())
    assert first is not second
