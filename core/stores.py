"""执行状态端口及单进程开发实现。

生产实现应使用持久化数据库/事件存储，并以 (tenant_id, request_id) 唯一约束保证并发幂等。
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import sqlite3
import time
from typing import Protocol
from uuid import uuid4

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
    producer_task: asyncio.Task[object] | None = field(default=None, repr=False)


class IdempotencyConflict(Exception):
    pass


class ExecutionStore(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def create_or_get(self, record: ExecutionRecord) -> tuple[ExecutionRecord, bool]: ...

    async def get_by_request_id(self, tenant_id: str, request_id: str) -> ExecutionRecord | None: ...

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

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

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

    async def get_by_request_id(
        self, tenant_id: str, request_id: str
    ) -> ExecutionRecord | None:
        async with self._lock:
            return self._by_request.get((tenant_id, request_id))

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
            if _terminal_event(event):
                return

    async def wait_terminal(self, record: ExecutionRecord) -> KemoResponse:
        async with record.condition:
            await record.condition.wait_for(
                lambda: record.status in TERMINAL_STATUSES and record.response is not None
            )
            assert record.response is not None
            return record.response


class SQLiteExecutionStore:
    """Durable single-node execution and SSE event store.

    SQLite WAL supplies the durable idempotency boundary.  Runtime conditions
    remain in memory because ``start_web.py`` deliberately runs one worker;
    after a restart, interrupted producers are converted into a deterministic
    ``incomplete`` terminal response and all already-written events remain
    replayable through ``Last-Event-ID``.
    """

    def __init__(
        self,
        root: Path,
        *,
        retention_hours: int = 24,
        cleanup_interval_seconds: float = 3600.0,
        max_events_per_response: int = 200_000,
    ) -> None:
        self.root = root.resolve()
        self.path = self.root / "executions.sqlite3"
        self.retention_seconds = max(1, retention_hours) * 3600
        self.cleanup_interval_seconds = max(60.0, cleanup_interval_seconds)
        self.max_events_per_response = max(100, max_events_per_response)
        self._lock = asyncio.Lock()
        self._by_request: dict[tuple[str, str], ExecutionRecord] = {}
        self._by_response: dict[tuple[str, str], ExecutionRecord] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)
        await asyncio.to_thread(self._recover_interrupted_sync)
        await self.cleanup_expired()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(), name="execution-store-cleanup"
            )

    async def close(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            await self.cleanup_expired()

    async def cleanup_expired(self) -> None:
        cutoff = time.time() - self.retention_seconds
        async with self._lock:
            removed = await asyncio.to_thread(self._cleanup_expired_sync, cutoff)
            for key in removed:
                record = self._by_request.pop(key, None)
                if record is not None:
                    self._by_response.pop((record.tenant_id, record.response_id), None)

    async def create_or_get(
        self, record: ExecutionRecord
    ) -> tuple[ExecutionRecord, bool]:
        key = (record.tenant_id, record.request_id)
        async with self._lock:
            cached = self._by_request.get(key)
            if cached is not None:
                if cached.request_hash != record.request_hash:
                    raise IdempotencyConflict(record.request_id)
                return cached, False
            row, created = await asyncio.to_thread(self._create_or_get_sync, record)
            resolved = record if created else await asyncio.to_thread(self._hydrate_sync, row)
            if resolved.request_hash != record.request_hash:
                raise IdempotencyConflict(record.request_id)
            self._cache(resolved)
            return resolved, created

    async def get_by_request_id(
        self, tenant_id: str, request_id: str
    ) -> ExecutionRecord | None:
        key = (tenant_id, request_id)
        async with self._lock:
            cached = self._by_request.get(key)
            if cached is not None:
                return cached
            row = await asyncio.to_thread(
                self._select_one_sync,
                "SELECT * FROM executions WHERE tenant_id = ? AND request_id = ?",
                (tenant_id, request_id),
            )
            if row is None:
                return None
            record = await asyncio.to_thread(self._hydrate_sync, row)
            self._cache(record)
            return record

    async def get_by_response_id(
        self, tenant_id: str, response_id: str
    ) -> ExecutionRecord | None:
        key = (tenant_id, response_id)
        async with self._lock:
            cached = self._by_response.get(key)
            if cached is not None:
                return cached
            row = await asyncio.to_thread(
                self._select_one_sync,
                "SELECT * FROM executions WHERE tenant_id = ? AND response_id = ?",
                (tenant_id, response_id),
            )
            if row is None:
                return None
            record = await asyncio.to_thread(self._hydrate_sync, row)
            self._cache(record)
            return record

    async def save(self, record: ExecutionRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, record)
            self._cache(record)
        async with record.condition:
            record.condition.notify_all()

    async def append_event(self, record: ExecutionRecord, event: SSEEvent) -> None:
        async with self._lock:
            expected = len(record.events)
            if event.sequence != expected:
                raise RuntimeError(
                    f"SSE sequence 应为 {expected}，实际为 {event.sequence}"
                )
            if expected >= self.max_events_per_response or (
                expected == self.max_events_per_response - 1
                and not _terminal_event(event)
            ):
                raise RuntimeError("SSE 事件数量超过单响应安全上限")
            await asyncio.to_thread(self._append_event_sync, record, event)
            record.events.append(event)
            self._cache(record)
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
            if _terminal_event(event):
                return

    async def wait_terminal(self, record: ExecutionRecord) -> KemoResponse:
        async with record.condition:
            await record.condition.wait_for(
                lambda: record.status in TERMINAL_STATUSES
                and record.response is not None
            )
            assert record.response is not None
            return record.response

    def _cache(self, record: ExecutionRecord) -> None:
        self._by_request[(record.tenant_id, record.request_id)] = record
        self._by_response[(record.tenant_id, record.response_id)] = record

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    tenant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    live_config_revision TEXT NOT NULL,
                    gateway_system_prompt_hash TEXT,
                    status TEXT NOT NULL,
                    provider_response_id TEXT,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, request_id),
                    UNIQUE (tenant_id, response_id)
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    tenant_id TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, response_id, sequence),
                    UNIQUE (tenant_id, response_id, event_id),
                    FOREIGN KEY (tenant_id, response_id)
                        REFERENCES executions (tenant_id, response_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_executions_updated
                    ON executions(updated_at);
                """
            )
            connection.commit()

    def _recover_interrupted_sync(self) -> None:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM executions WHERE status IN ('created', 'running')"
            ).fetchall()
            now = time.time()
            for row in rows:
                response = KemoResponse(
                    id=row["response_id"],
                    request_id=row["request_id"],
                    status="incomplete",
                    model=row["model"],
                    incomplete_details={"reason": "gateway_restarted"},
                )
                sequence = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(sequence), -1) + 1
                           FROM execution_events
                           WHERE tenant_id = ? AND response_id = ?""",
                        (row["tenant_id"], row["response_id"]),
                    ).fetchone()[0]
                )
                event = SSEEvent(
                    type="response.incomplete",
                    event_id=f"evt_{uuid4().hex}",
                    sequence=sequence,
                    request_id=row["request_id"],
                    response_id=row["response_id"],
                    timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    response=response,
                )
                connection.execute(
                    """INSERT INTO execution_events
                       (tenant_id, response_id, sequence, event_id, event_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        row["tenant_id"],
                        row["response_id"],
                        sequence,
                        event.event_id,
                        event.model_dump_json(exclude_none=True),
                        now,
                    ),
                )
                connection.execute(
                    """UPDATE executions
                       SET status = 'incomplete', response_json = ?, updated_at = ?
                       WHERE tenant_id = ? AND request_id = ?""",
                    (
                        response.model_dump_json(exclude_none=True),
                        now,
                        row["tenant_id"],
                        row["request_id"],
                    ),
                )
            connection.commit()

    def _create_or_get_sync(
        self, record: ExecutionRecord
    ) -> tuple[sqlite3.Row, bool]:
        now = time.time()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO executions
                   (tenant_id, request_id, request_hash, response_id, model,
                    provider_id, subject_id, live_config_revision,
                    gateway_system_prompt_hash, status, provider_response_id,
                    response_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.tenant_id,
                    record.request_id,
                    record.request_hash,
                    record.response_id,
                    record.model,
                    record.provider_id,
                    record.subject_id,
                    record.live_config_revision,
                    record.gateway_system_prompt_hash,
                    record.status.value,
                    record.provider_response_id,
                    self._response_json(record.response),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE tenant_id = ? AND request_id = ?",
                (record.tenant_id, record.request_id),
            ).fetchone()
            connection.commit()
            assert row is not None
            return row, cursor.rowcount == 1

    def _save_sync(self, record: ExecutionRecord) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE executions
                   SET status = ?, provider_response_id = ?, response_json = ?,
                       updated_at = ?
                   WHERE tenant_id = ? AND request_id = ?""",
                (
                    record.status.value,
                    record.provider_response_id,
                    self._response_json(record.response),
                    time.time(),
                    record.tenant_id,
                    record.request_id,
                ),
            )
            connection.commit()

    def _append_event_sync(self, record: ExecutionRecord, event: SSEEvent) -> None:
        with closing(self._connect()) as connection:
            expected = int(
                connection.execute(
                    """SELECT COALESCE(MAX(sequence), -1) + 1
                       FROM execution_events
                       WHERE tenant_id = ? AND response_id = ?""",
                    (record.tenant_id, record.response_id),
                ).fetchone()[0]
            )
            if event.sequence != expected:
                raise RuntimeError(
                    f"持久化 SSE sequence 应为 {expected}，实际为 {event.sequence}"
                )
            connection.execute(
                """INSERT INTO execution_events
                   (tenant_id, response_id, sequence, event_id, event_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.tenant_id,
                    record.response_id,
                    event.sequence,
                    event.event_id,
                    event.model_dump_json(exclude_none=True),
                    time.time(),
                ),
            )
            if _terminal_event(event) and event.response is not None:
                connection.execute(
                    """UPDATE executions
                       SET status = ?, provider_response_id = ?,
                           response_json = ?, updated_at = ?
                       WHERE tenant_id = ? AND response_id = ?""",
                    (
                        event.response.status,
                        event.response.provider_response_id,
                        event.response.model_dump_json(exclude_none=True),
                        time.time(),
                        record.tenant_id,
                        record.response_id,
                    ),
                )
            connection.commit()

    def _hydrate_sync(self, row: sqlite3.Row) -> ExecutionRecord:
        response = (
            KemoResponse.model_validate_json(row["response_json"])
            if row["response_json"]
            else None
        )
        with closing(self._connect()) as connection:
            event_rows = connection.execute(
                """SELECT event_json FROM execution_events
                   WHERE tenant_id = ? AND response_id = ? ORDER BY sequence""",
                (row["tenant_id"], row["response_id"]),
            ).fetchall()
        events = [SSEEvent.model_validate_json(item["event_json"]) for item in event_rows]
        return ExecutionRecord(
            tenant_id=row["tenant_id"],
            request_id=row["request_id"],
            request_hash=row["request_hash"],
            response_id=row["response_id"],
            model=row["model"],
            provider_id=row["provider_id"],
            subject_id=row["subject_id"],
            live_config_revision=row["live_config_revision"],
            gateway_system_prompt_hash=row["gateway_system_prompt_hash"],
            status=InternalStatus(row["status"]),
            provider_response_id=row["provider_response_id"],
            response=response,
            events=events,
        )

    def _select_one_sync(
        self, query: str, parameters: tuple[str, str]
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(query, parameters).fetchone()

    def _cleanup_expired_sync(self, cutoff: float) -> list[tuple[str, str]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT tenant_id, request_id FROM executions
                   WHERE updated_at < ? AND status NOT IN ('created', 'running')""",
                (cutoff,),
            ).fetchall()
            connection.execute(
                """DELETE FROM executions
                   WHERE updated_at < ? AND status NOT IN ('created', 'running')""",
                (cutoff,),
            )
            connection.commit()
        return [(row["tenant_id"], row["request_id"]) for row in rows]

    @staticmethod
    def _response_json(response: KemoResponse | None) -> str | None:
        return response.model_dump_json(exclude_none=True) if response is not None else None


def _terminal_event(event: SSEEvent) -> bool:
    return event.type in {
        "response.completed",
        "response.incomplete",
        "response.failed",
        "response.cancelled",
        "error",
    }
