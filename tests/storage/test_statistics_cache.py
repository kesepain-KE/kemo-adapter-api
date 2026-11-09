from __future__ import annotations

import asyncio
import threading
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from storage import read_cache
from storage.read_cache import ReadCache
from storage.statistics import StatisticsStore
from core.models import Usage


def test_cache_ttl_is_fixed_and_values_are_isolated(monkeypatch) -> None:
    now = [10.0]
    monkeypatch.setattr(read_cache.time, "monotonic", lambda: now[0])
    cache = ReadCache(ttl_seconds=5)
    original = {"tokens": {"total": 3}}
    cache.put("a", original)
    original["tokens"]["total"] = 9
    hit = cache.get("a")
    assert hit == {"tokens": {"total": 3}}
    hit["tokens"]["total"] = 99
    now[0] = 14.9
    assert cache.get("a") == {"tokens": {"total": 3}}
    now[0] = 15.0
    assert cache.get("a") is None


def test_cache_is_lru_bounded_by_entries_and_payload_bytes() -> None:
    cache = ReadCache(max_entries=2, max_bytes=20)
    cache.put("a", {"n": 1})
    cache.put("b", {"n": 2})
    assert cache.get("a") == {"n": 1}
    cache.put("c", {"n": 3})
    assert cache.get("b") is None
    assert cache._bytes <= 20
    cache.put("a", {"large": "x" * 100})
    assert cache.get("a") is None
    cache.clear()
    assert cache._bytes == 0
    assert not cache._entries
    byte_limited = ReadCache(max_entries=100, max_bytes=10)
    byte_limited.put("a", {"n": 1})
    byte_limited.put("b", {"n": 2})
    assert byte_limited.get("a") is None
    assert byte_limited.get("b") == {"n": 2}


@pytest.mark.parametrize("options", [
    {"ttl_seconds": 0}, {"max_entries": 0}, {"max_bytes": 0},
])
def test_cache_can_be_disabled(options) -> None:
    cache = ReadCache(**options)
    cache.put("a", {"n": 1})
    assert cache.get("a") is None


@pytest.mark.parametrize("operation,method,args", [
    ("daily", "_daily_sync", ("2026-09-13",)),
    ("hourly", "_hourly_sync", ("2026-09-13",)),
    ("rankings", "_rankings_sync", ("2026-09-13", "model")),
    ("series", "_series_sync", ("2026-09-07", "2026-09-13")),
    ("gateway_key_usage", "_gateway_key_usage_sync", ()),
    ("recent_invocations", "_recent_invocations_sync", ()),
])
def test_repeated_and_concurrent_queries_only_read_database_once(
    tmp_path: Path, monkeypatch, operation, method, args,
) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path)
        original = getattr(store, method)
        calls = []

        def counted(*values):
            calls.append(values)
            return original(*values)

        monkeypatch.setattr(store, method, counted)
        query = getattr(store, operation)
        results = await asyncio.gather(*(query(*args) for _ in range(20)))
        assert all(result == results[0] for result in results)
        assert len(calls) == 1
        # HTTP response decoration must not alter later cache hits.
        results[0]["request_only"] = True
        assert "request_only" not in await query(*args)

    asyncio.run(scenario())


def test_statistics_writes_invalidate_all_views_immediately(tmp_path: Path) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path)
        await store.initialize()
        day = datetime.now(store.timezone).date().isoformat()
        assert (await store.daily(day))["calls"] == 0
        assert (await store.recent_invocations())["total"] == 0
        assert await store.gateway_key_usage() == {}
        handle = await store.begin_invocation(
            task="llm", provider_id="test", model="test-model", tenant_id="test",
            gateway_key_id="test-key", request_id="test-request",
        )
        assert handle is not None
        assert (await store.daily(day))["calls"] == 1
        assert (await store.recent_invocations())["total"] == 1
        assert (await store.daily(day))["successes"] == 0
        await store.finish_invocation(handle, status="completed")
        assert (await store.daily(day))["successes"] == 1
        assert (await store.gateway_key_usage())["test-key"]["successes"] == 1
        assert (await store.recent_invocations("success"))["total"] == 1
        await store.record_replay(
            task="llm", provider_id="test", model="test-model", gateway_key_id="test-key",
        )
        assert (await store.daily(day))["replay_count"] == 1
        # Persisted results remain available after a new store is constructed.
        fresh = StatisticsStore(tmp_path)
        assert (await fresh.daily(day))["successes"] == 1

    asyncio.run(scenario())


def test_query_parameters_and_store_roots_do_not_share_cache(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path / "first")
        other = StatisticsStore(tmp_path / "second")
        calls = []

        def query(*args):
            calls.append(args)
            return {"args": list(args)}

        monkeypatch.setattr(store, "_recent_invocations_sync", query)
        await store.recent_invocations(day="2026-09-13", hour=1)
        await store.recent_invocations(day="2026-09-13", hour=2)
        await store.recent_invocations(day="2026-09-13", hour=2, offset=20)
        await store.recent_invocations("failure", day="2026-09-13", hour=2, offset=20)
        await store.recent_invocations("failure", day="2026-09-13", hour=2, offset=20, limit=20)
        await store.recent_invocations(day="2026-09-14", hour=1)
        assert len(calls) == 6
        assert (await other.recent_invocations())["total"] == 0

    asyncio.run(scenario())


def test_read_errors_are_not_cached_and_failed_writes_clear_cache(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path, write_batch_size=1)
        calls = []

        def fail(*args):
            calls.append(args)
            raise OSError("simulated disk error")

        original = store._daily_sync
        monkeypatch.setattr(store, "_daily_sync", fail)
        for _ in range(2):
            with pytest.raises(OSError):
                await store.daily("2026-09-13")
        assert len(calls) == 2
        monkeypatch.setattr(store, "_daily_sync", original)
        await store.daily("2026-09-13")
        assert store._read_cache._entries
        monkeypatch.setattr(store, "_record_replay_sync", fail)
        await store.record_replay(task="llm", provider_id="test", model="test", gateway_key_id=None)
        assert not store._read_cache._entries
        assert store.health()["healthy"] is False

    asyncio.run(scenario())


def test_cancelled_write_keeps_lock_until_thread_finishes(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path, write_batch_size=1)
        entered = threading.Event()
        release = threading.Event()
        original = store._record_replay_sync

        def blocked(*args):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test release was not signalled")
            return original(*args)

        monkeypatch.setattr(store, "_record_replay_sync", blocked)
        writer = asyncio.create_task(store.record_replay(
            task="llm", provider_id="test", model="test", gateway_key_id=None,
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            writer.cancel()
            await asyncio.sleep(0)
            writer.cancel()
            await asyncio.sleep(0)
            assert store._lock.locked()
            reader = asyncio.create_task(store.daily(datetime.now(store.timezone).date()))
            await asyncio.sleep(0)
            assert not reader.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert (await reader)["replay_count"] == 1
        assert not store._lock.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize("batch_size,expected_commits", [(128, 1), (8, 5)])
def test_statistics_batch_commits_and_queue_bound(
    tmp_path: Path, monkeypatch, batch_size: int, expected_commits: int,
) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path, write_batch_size=batch_size, write_flush_interval_seconds=60)
        await store.initialize()
        commits = []
        connect = store._connect

        def counted(path):
            connection = connect(path)
            connection.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
            return connection

        monkeypatch.setattr(store, "_connect", counted)
        usage = Usage(input_tokens=10, output_tokens=2, total_tokens=12)
        for index in range(20):
            handle = await store.begin_invocation(
                task="llm", provider_id="test", model="model", tenant_id="tenant",
                gateway_key_id=None, request_id=f"request-{index}",
            )
            assert handle is not None
            await store.finish_invocation(handle, status="completed", usage=usage)
            assert store.health()["pending_events"] < batch_size
        # Queued writes own their usage snapshot.
        usage.total_tokens = 999
        await store.flush()
        assert len(commits) == expected_commits
        with sqlite3.connect(handle.database) as connection:
            assert connection.execute(
                "SELECT COUNT(*), SUM(total_tokens) FROM invocations WHERE status='completed'",
            ).fetchone() == (20, 240)
        fresh = StatisticsStore(tmp_path)
        daily = await fresh.daily(handle.day)
        assert daily["calls"] == daily["successes"] == 20
        assert daily["running"] == 0 and daily["tokens"]["total_tokens"] == 240
        await fresh.close()
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("trigger", ["timer", "close"])
def test_statistics_flushes_on_timer_and_close(tmp_path: Path, monkeypatch, trigger: str) -> None:
    async def scenario():
        store = StatisticsStore(
            tmp_path, write_flush_interval_seconds=.01 if trigger == "timer" else 60,
        )
        await store.initialize()
        flushed = asyncio.Event()
        loop = asyncio.get_running_loop()
        flush = store._flush_sync

        def observed(pending):
            flush(pending)
            loop.call_soon_threadsafe(flushed.set)

        monkeypatch.setattr(store, "_flush_sync", observed)
        await store.record_replay(task="llm", provider_id="test", model="model", gateway_key_id=None)
        assert store.health()["pending_events"] == 1
        if trigger == "close":
            await store.close()
        else:
            await asyncio.wait_for(flushed.wait(), timeout=3)
        fresh = StatisticsStore(tmp_path)
        assert (await fresh.daily(datetime.now(store.timezone).date()))["replay_count"] == 1
        assert store.health()["pending_events"] == 0
        await fresh.close()
        await store.close()

    asyncio.run(scenario())


def test_failed_batch_rolls_back_and_reports_dropped_events(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        store = StatisticsStore(tmp_path, write_flush_interval_seconds=60)
        await store.initialize()
        handle = await store.begin_invocation(
            task="llm", provider_id="test", model="model", tenant_id="tenant",
            gateway_key_id=None, request_id="failed-batch",
        )

        def fail(*args):
            raise sqlite3.OperationalError("simulated write failure")

        monkeypatch.setattr(store, "_finish_sync", fail)
        await store.finish_invocation(handle, status="completed")
        await store.flush()
        assert store.health()["healthy"] is False
        assert store.health()["dropped_events"] == 2
        with sqlite3.connect(handle.database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        await store.close()

    asyncio.run(scenario())
