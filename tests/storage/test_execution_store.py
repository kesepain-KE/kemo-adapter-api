from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest

from core.event_assembler import EventAssembler
from core.models import KemoResponse, SSEEvent
from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderResult
from core.stores import (
    ExecutionRecord,
    IdempotencyConflict,
    InternalStatus,
    SQLiteExecutionStore,
)


def _record(
    *,
    request_hash: str = "hash-1",
    request_id: str = "request-1",
    response_id: str = "response-1",
) -> ExecutionRecord:
    return ExecutionRecord(
        tenant_id="tenant-1",
        request_id=request_id,
        request_hash=request_hash,
        response_id=response_id,
        model="provider-model",
        provider_id="provider",
        subject_id="subject-1",
    )


def _reasoning_event(record: ExecutionRecord, sequence: int) -> SSEEvent:
    return EventAssembler.assemble(
        ProviderEvent(
            kind=ProviderEventKind.REASONING_CONTENT_DELTA,
            item_id="reasoning-1",
            delta=f"delta-{sequence}",
        ),
        request_id=record.request_id,
        response_id=record.response_id,
        sequence=sequence,
    )


class _CountingSQLiteExecutionStore(SQLiteExecutionStore):
    def __init__(self, root: Path, **kwargs: object) -> None:
        super().__init__(root, **kwargs)
        self.persisted_batches: list[list[int]] = []

    def _append_events_sync(
        self,
        record: ExecutionRecord,
        events: list[SSEEvent],
        persist_record: bool = False,
    ) -> None:
        self.persisted_batches.append([event.sequence for event in events])
        super()._append_events_sync(record, events, persist_record)


class _BlockingSQLiteExecutionStore(SQLiteExecutionStore):
    def __init__(self, root: Path) -> None:
        super().__init__(
            root,
            event_flush_interval_seconds=60,
            event_batch_size=1,
        )
        self.write_started = threading.Event()
        self.allow_write = threading.Event()

    def _append_events_sync(
        self,
        record: ExecutionRecord,
        events: list[SSEEvent],
        persist_record: bool = False,
    ) -> None:
        self.write_started.set()
        if not self.allow_write.wait(timeout=2):
            raise TimeoutError("test did not release SQLite write")
        super()._append_events_sync(record, events, persist_record)


def test_sqlite_execution_store_persists_idempotency_and_terminal_response(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await first.initialize()
        record, created = await first.create_or_get(_record())
        assert created is True
        response = KemoResponse(
            id=record.response_id,
            request_id=record.request_id,
            status="incomplete",
            model=record.model,
            incomplete_details={"reason": "test"},
        )
        record.status = InternalStatus.INCOMPLETE
        record.response = response
        await first.save(record)
        await first.close()

        second = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await second.initialize()
        replay, replay_created = await second.create_or_get(_record())
        assert replay_created is False
        assert replay.response == response
        with pytest.raises(IdempotencyConflict):
            await second.create_or_get(_record(request_hash="different"))
        await second.close()

    asyncio.run(scenario())


def test_sqlite_execution_store_recovers_interrupted_stream_for_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await first.initialize()
        record, _ = await first.create_or_get(_record())
        record.status = InternalStatus.RUNNING
        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        await first.append_event(record, created)
        await first.save(record)
        await first.close()

        second = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await second.initialize()
        recovered = await second.get_by_request_id("tenant-1", "request-1")
        assert recovered is not None
        assert recovered.status == InternalStatus.INCOMPLETE
        assert recovered.response is not None
        assert recovered.response.incomplete_details == {"reason": "gateway_restarted"}
        assert [event.type for event in recovered.events] == [
            "response.created",
            "response.incomplete",
        ]
        replay = [
            event
            async for event in second.subscribe(
                recovered, after_sequence=created.sequence
            )
        ]
        assert [event.type for event in replay] == ["response.incomplete"]
        await second.close()

    asyncio.run(scenario())


def test_terminal_event_and_response_commit_atomically(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await first.initialize()
        record, _ = await first.create_or_get(_record())
        record.status = InternalStatus.RUNNING
        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        await first.append_event(record, created)
        response = KemoResponse(
            id=record.response_id,
            request_id=record.request_id,
            status="incomplete",
            model=record.model,
            incomplete_details={"reason": "finished-before-save"},
        )
        terminal = EventAssembler.assemble(
            ProviderEvent(
                kind=ProviderEventKind.INCOMPLETE,
                result=ProviderResult(status="incomplete"),
            ),
            request_id=record.request_id,
            response_id=record.response_id,
            sequence=1,
            terminal_response=response,
        )
        # Simulate a crash after the event transaction but before store.save().
        await first.append_event(record, terminal)
        await first.close()

        second = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await second.initialize()
        recovered = await second.get_by_request_id("tenant-1", "request-1")
        assert recovered is not None
        assert recovered.response == response
        assert [event.type for event in recovered.events] == [
            "response.created",
            "response.incomplete",
        ]
        await second.close()

    asyncio.run(scenario())


def test_non_terminal_events_are_batched_in_memory_before_publish(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = _CountingSQLiteExecutionStore(
            tmp_path,
            event_flush_interval_seconds=60,
            event_batch_size=3,
        )
        await store.initialize()
        record, _ = await store.create_or_get(_record())

        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        await store.append_event(record, created)
        await store.append_event(record, _reasoning_event(record, 1))

        assert record.events == []
        assert [event.sequence for event in record.pending_events] == [0, 1]
        assert record.next_sequence == 2
        assert store.persisted_batches == []

        await store.append_event(record, _reasoning_event(record, 2))

        assert [event.sequence for event in record.events] == [0, 1, 2]
        assert record.pending_events == []
        assert record.persisted_sequence == 2
        assert store.persisted_batches == [[0, 1, 2]]
        await store.close()

    asyncio.run(scenario())


def test_subscriber_only_sees_events_after_delayed_commit(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteExecutionStore(
            tmp_path,
            event_flush_interval_seconds=0.02,
            event_batch_size=32,
        )
        await store.initialize()
        record, _ = await store.create_or_get(_record())
        subscriber = asyncio.create_task(anext(store.subscribe(record)))

        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        await store.append_event(record, created)

        assert record.events == []
        assert subscriber.done() is False
        published = await asyncio.wait_for(subscriber, timeout=1)
        assert published == created
        assert record.events == [created]
        assert record.pending_events == []
        await store.close()

    asyncio.run(scenario())


def test_terminal_event_flushes_pending_batch_and_publishes_state_atomically(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = _CountingSQLiteExecutionStore(
            tmp_path,
            event_flush_interval_seconds=60,
            event_batch_size=32,
        )
        await store.initialize()
        record, _ = await store.create_or_get(_record())
        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        await store.append_event(record, created)
        response = KemoResponse(
            id=record.response_id,
            request_id=record.request_id,
            status="incomplete",
            model=record.model,
            incomplete_details={"reason": "batched-terminal"},
        )
        record.response = response
        terminal = EventAssembler.assemble(
            ProviderEvent(
                kind=ProviderEventKind.INCOMPLETE,
                result=ProviderResult(status="incomplete"),
            ),
            request_id=record.request_id,
            response_id=record.response_id,
            sequence=record.next_sequence,
            terminal_response=response,
        )

        await store.append_event(record, terminal)

        assert store.persisted_batches == [[0, 1]]
        assert [event.type for event in record.events] == [
            "response.created",
            "response.incomplete",
        ]
        assert record.pending_events == []
        assert record.status == InternalStatus.INCOMPLETE
        assert record.response == response
        await store.close()

        replay_store = SQLiteExecutionStore(tmp_path, retention_hours=24)
        await replay_store.initialize()
        replay = await replay_store.get_by_request_id("tenant-1", "request-1")
        assert replay is not None
        assert replay.status == InternalStatus.INCOMPLETE
        assert replay.response == response
        assert [event.type for event in replay.events] == [
            "response.created",
            "response.incomplete",
        ]
        await replay_store.close()

    asyncio.run(scenario())


def test_pending_sequence_is_reserved_before_batch_commit(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteExecutionStore(
            tmp_path,
            event_flush_interval_seconds=60,
            event_batch_size=32,
        )
        await store.initialize()
        record, _ = await store.create_or_get(_record())
        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        await store.append_event(record, created)

        with pytest.raises(RuntimeError, match="SSE sequence 应为 1"):
            await store.append_event(record, _reasoning_event(record, 0))
        assert record.next_sequence == 1
        await store.close()

    asyncio.run(scenario())


def test_terminal_flush_is_isolated_per_response(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _CountingSQLiteExecutionStore(
            tmp_path,
            event_flush_interval_seconds=60,
            event_batch_size=32,
        )
        await store.initialize()
        first, _ = await store.create_or_get(
            _record(request_id="request-a", response_id="response-a")
        )
        second, _ = await store.create_or_get(
            _record(request_id="request-b", response_id="response-b")
        )
        await store.append_event(
            first,
            EventAssembler.created(
                request_id=first.request_id,
                response_id=first.response_id,
            ),
        )
        await store.append_event(
            second,
            EventAssembler.created(
                request_id=second.request_id,
                response_id=second.response_id,
            ),
        )
        response = KemoResponse(
            id=first.response_id,
            request_id=first.request_id,
            status="incomplete",
            model=first.model,
            incomplete_details={"reason": "isolated"},
        )
        first.response = response
        terminal = EventAssembler.assemble(
            ProviderEvent(
                kind=ProviderEventKind.INCOMPLETE,
                result=ProviderResult(status="incomplete"),
            ),
            request_id=first.request_id,
            response_id=first.response_id,
            sequence=first.next_sequence,
            terminal_response=response,
        )
        await store.append_event(first, terminal)

        assert [event.sequence for event in first.events] == [0, 1]
        assert second.events == []
        assert [event.sequence for event in second.pending_events] == [0]
        assert store.persisted_batches == [[0, 1]]
        await store.close()

    asyncio.run(scenario())


def test_cancelled_append_finishes_commit_before_exposing_cancellation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = _BlockingSQLiteExecutionStore(tmp_path)
        await store.initialize()
        record, _ = await store.create_or_get(_record())
        subscriber = asyncio.create_task(anext(store.subscribe(record)))
        created = EventAssembler.created(
            request_id=record.request_id,
            response_id=record.response_id,
        )
        append_task = asyncio.create_task(store.append_event(record, created))
        started = await asyncio.to_thread(store.write_started.wait, 1)
        assert started is True

        append_task.cancel()
        store.allow_write.set()
        with pytest.raises(asyncio.CancelledError):
            await append_task

        assert record.events == [created]
        assert record.pending_events == []
        assert record.persisted_sequence == 0
        assert await asyncio.wait_for(subscriber, timeout=1) == created
        await store.close()

        replay_store = SQLiteExecutionStore(tmp_path)
        await replay_store.initialize()
        replay = await replay_store.get_by_request_id("tenant-1", "request-1")
        assert replay is not None
        assert replay.events[0] == created
        await replay_store.close()

    asyncio.run(scenario())
