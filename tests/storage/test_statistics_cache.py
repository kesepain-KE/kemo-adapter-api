from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path

import pytest

from storage import read_cache
from storage.read_cache import ReadCache
from storage.statistics import StatisticsStore


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
        store = StatisticsStore(tmp_path)
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
        store = StatisticsStore(tmp_path)
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
