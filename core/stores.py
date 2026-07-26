"""执行状态端口及单进程开发实现。

生产实现应使用持久化数据库/事件存储，并以 (tenant_id, request_id) 唯一约束保证并发幂等。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from core.models import KemoResponse, SSEEvent


class InternalStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    REQUIRES_ACTION = "requires_action"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    InternalStatus.COMPLETED,
    InternalStatus.REQUIRES_ACTION,
    InternalStatus.INCOMPLETE,
    InternalStatus.FAILED,
    InternalStatus.CANCELLED,
}


@dataclass(slots=True)
class ExecutionRecord:
    tenant_id: str
    request_id: str
    request_hash: str
    response_id: str
    model: str
    provider_id: str
    subject_id: str
    live_config_revision: str = "empty"
    gateway_system_prompt_hash: str | None = None
    status: InternalStatus = InternalStatus.CREATED
    provider_response_id: str | None = None
    response: KemoResponse | None = None
    events: list[SSEEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    producer_task: asyncio.Task[None] | None = field(default=None, repr=False)


class IdempotencyConflict(Exception):
    pass


class ExecutionStore(Protocol):
    async def create_or_get(self, record: ExecutionRecord) -> tuple[ExecutionRecord, bool]: ...

    async def get_by_response_id(self, tenant_id: str, response_id: str) -> ExecutionRecord | None: ...

    async def save(self, record: ExecutionRecord) -> None: ...

    async def append_event(self, record: ExecutionRecord, event: SSEEvent) -> None: ...

    def subscribe(self, record: ExecutionRecord, after_sequence: int = -1) -> AsyncIterator[SSEEvent]: ...

    async def wait_terminal(self, record: ExecutionRecord) -> KemoResponse: ...


class InMemoryExecutionStore:
    """只用于开发/测试；接口刻意与持久化实现保持一致。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_request: dict[tuple[str, str], ExecutionRecord] = {}
        self._by_response: dict[tuple[str, str], ExecutionRecord] = {}

    async def create_or_get(self, record: ExecutionRecord) -> tuple[ExecutionRecord, bool]:
        key = (record.tenant_id, record.request_id)
        async with self._lock:
            existing = self._by_request.get(key)
            if existing is not None:
                if existing.request_hash != record.request_hash:
                    raise IdempotencyConflict(record.request_id)
                return existing, False
            self._by_request[key] = record
            self._by_response[(record.tenant_id, record.response_id)] = record
            return record, True

    async def get_by_response_id(self, tenant_id: str, response_id: str) -> ExecutionRecord | None:
        async with self._lock:
            return self._by_response.get((tenant_id, response_id))

    async def save(self, record: ExecutionRecord) -> None:
        async with self._lock:
            self._by_request[(record.tenant_id, record.request_id)] = record
            self._by_response[(record.tenant_id, record.response_id)] = record
        async with record.condition:
            record.condition.notify_all()

    async def append_event(self, record: ExecutionRecord, event: SSEEvent) -> None:
        async with self._lock:
            expected = len(record.events)
            if event.sequence != expected:
                raise RuntimeError(f"SSE sequence 应为 {expected}，实际为 {event.sequence}")
            record.events.append(event)
        async with record.condition:
            record.condition.notify_all()

    async def subscribe(
        self, record: ExecutionRecord, after_sequence: int = -1
    ) -> AsyncIterator[SSEEvent]:
        next_sequence = after_sequence + 1
        while True:
            async with record.condition:
                await record.condition.wait_for(
                    lambda: next_sequence < len(record.events)
                    or record.status in TERMINAL_STATUSES
                )
                if next_sequence >= len(record.events):
                    return
                event = record.events[next_sequence]
            next_sequence += 1
            yield event
            if event.type in {
                "response.completed",
                "response.incomplete",
                "response.failed",
                "response.cancelled",
                "error",
            }:
                return

    async def wait_terminal(self, record: ExecutionRecord) -> KemoResponse:
        async with record.condition:
            await record.condition.wait_for(
                lambda: record.status in TERMINAL_STATUSES and record.response is not None
            )
            assert record.response is not None
            return record.response
