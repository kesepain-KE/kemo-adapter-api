from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.event_assembler import EventAssembler
from core.models import KemoResponse
from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderResult
from core.stores import (
    ExecutionRecord,
    IdempotencyConflict,
    InternalStatus,
    SQLiteExecutionStore,
)


def _record(*, request_hash: str = "hash-1") -> ExecutionRecord:
    return ExecutionRecord(
        tenant_id="tenant-1",
        request_id="request-1",
        request_hash=request_hash,
        response_id="response-1",
        model="provider-model",
        provider_id="provider",
        subject_id="subject-1",
    )


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
