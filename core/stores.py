"""执行状态端口及单进程开发实现。

生产实现应使用持久化数据库/事件存储，并以 (tenant_id, request_id) 唯一约束保证并发幂等。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import logging
from pathlib import Path
import sqlite3
import time
from typing import Protocol
from uuid import uuid4

from core.models import KemoResponse, SSEEvent


logger = logging.getLogger(__name__)


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
    persisted_sequence: int = field(default=-1, repr=False)
    pending_events: list[SSEEvent] = field(default_factory=list, repr=False)
    pending_flush_task: asyncio.Task[None] | None = field(default=None, repr=False)
    pending_flush_error: BaseException | None = field(default=None, repr=False)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    producer_task: asyncio.Task[object] | None = field(default=None, repr=False)

    @property
    def next_sequence(self) -> int:
        """Return the next reserved sequence, including not-yet-published events."""
        return len(self.events) + len(self.pending_events)


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
            if record.status in TERMINAL_STATUSES:
                raise RuntimeError("终态 Execution 不得继续追加 SSE 事件")
            expected = record.next_sequence
            if event.sequence != expected:
                raise RuntimeError(f"SSE sequence 应为 {expected}，实际为 {event.sequence}")
            record.events.append(event)
            record.persisted_sequence = event.sequence
            _apply_terminal_event(record, event)
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
                    or record.pending_flush_error is not None
                )
                _raise_flush_error(record)
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
                lambda: (
                    record.status in TERMINAL_STATUSES and record.response is not None
                )
                or record.pending_flush_error is not None
            )
            _raise_flush_error(record)
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
        event_flush_interval_seconds: float = 0.05,
        event_batch_size: int = 32,
    ) -> None:
        self.root = root.resolve()
        self.path = self.root / "executions.sqlite3"
        self.retention_seconds = max(1, retention_hours) * 3600
        self.cleanup_interval_seconds = max(60.0, cleanup_interval_seconds)
        self.max_events_per_response = max(100, max_events_per_response)
        self.event_flush_interval_seconds = max(0.001, event_flush_interval_seconds)
        self.event_batch_size = max(1, event_batch_size)
        self._lock = asyncio.Lock()
        self._by_request: dict[tuple[str, str], ExecutionRecord] = {}
        self._by_response: dict[tuple[str, str], ExecutionRecord] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._connection: sqlite3.Connection | None = None

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
        notify: list[ExecutionRecord] = []
        delayed_cancel = False
        async with self._lock:
            records = list(self._by_request.values())
            for record in records:
                self._cancel_pending_flush(record)
                if record.pending_events:
                    delayed_cancel = (
                        await self._flush_pending_locked(record) or delayed_cancel
                    )
                    notify.append(record)
            delayed_cancel = (
                await self._run_critical_write(self._close_connection_sync)
                or delayed_cancel
            )
        for record in notify:
            await self._notify(record)
        if delayed_cancel:
            raise asyncio.CancelledError

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
        notify = False
        delayed_cancel = False
        async with self._lock:
            self._raise_pending_flush_error(record)
            self._cancel_pending_flush(record)
            if record.pending_events:
                delayed_cancel = await self._flush_pending_locked(
                    record,
                    persist_record=True,
                )
                notify = True
            else:
                delayed_cancel = await self._run_critical_write(
                    self._save_sync,
                    record,
                )
            self._cache(record)
        if notify or not record.pending_events:
            await self._notify(record)
        if delayed_cancel:
            raise asyncio.CancelledError

    async def append_event(self, record: ExecutionRecord, event: SSEEvent) -> None:
        notify = False
        delayed_cancel = False
        async with self._lock:
            self._raise_pending_flush_error(record)
            if record.status in TERMINAL_STATUSES:
                raise RuntimeError("终态 Execution 不得继续追加 SSE 事件")
            expected = record.next_sequence
            if event.sequence != expected:
                raise RuntimeError(
                    f"SSE sequence 应为 {expected}，实际为 {event.sequence}"
                )
            if expected >= self.max_events_per_response or (
                expected == self.max_events_per_response - 1
                and not _terminal_event(event)
            ):
                raise RuntimeError("SSE 事件数量超过单响应安全上限")
            record.pending_events.append(event)
            if _terminal_event(event) or len(record.pending_events) >= self.event_batch_size:
                self._cancel_pending_flush(record)
                delayed_cancel = await self._flush_pending_locked(record)
                notify = True
            elif record.pending_flush_task is None:
                record.pending_flush_task = asyncio.create_task(
                    self._flush_after_delay(record),
                    name=f"execution-event-flush:{record.response_id}",
                )
        if notify:
            await self._notify(record)
        if delayed_cancel:
            raise asyncio.CancelledError

    async def _flush_after_delay(self, record: ExecutionRecord) -> None:
        try:
            await asyncio.sleep(self.event_flush_interval_seconds)
            notify = False
            delayed_cancel = False
            async with self._lock:
                if record.pending_flush_task is not asyncio.current_task():
                    return
                record.pending_flush_task = None
                if record.pending_events:
                    delayed_cancel = await self._flush_pending_locked(record)
                    notify = True
            if notify:
                await self._notify(record)
            if delayed_cancel:
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            async with self._lock:
                record.pending_flush_error = exc
            logger.exception(
                "Background execution-event flush failed for %s",
                record.response_id,
            )
            await self._notify(record)

    async def _flush_pending_locked(
        self,
        record: ExecutionRecord,
        *,
        persist_record: bool = False,
    ) -> bool:
        if not record.pending_events:
            return False
        events = list(record.pending_events)
        delayed_cancel = await self._run_critical_write(
            self._append_events_sync,
            record,
            events,
            persist_record,
        )
        del record.pending_events[: len(events)]
        record.events.extend(events)
        record.persisted_sequence = events[-1].sequence
        for event in events:
            _apply_terminal_event(record, event)
        record.pending_flush_error = None
        self._cache(record)
        return delayed_cancel

    @staticmethod
    async def _run_critical_write(function: object, *args: object) -> bool:
        """Finish a started SQLite transaction before delivering cancellation.

        ``asyncio.to_thread`` cannot stop its worker when the awaiting task is
        cancelled.  Returning before the worker finishes could leave the
        database committed while the in-memory replay boundary still points
        at the previous event.  Consume cancellation temporarily, reconcile
        memory after the worker finishes, then let the caller re-raise it.
        """
        thread_task = asyncio.create_task(
            asyncio.to_thread(function, *args),  # type: ignore[arg-type]
        )
        delayed_cancel = False
        while True:
            try:
                await asyncio.shield(thread_task)
                return delayed_cancel
            except asyncio.CancelledError:
                delayed_cancel = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if thread_task.done():
                    thread_task.result()
                    return delayed_cancel

    def _cancel_pending_flush(self, record: ExecutionRecord) -> None:
        task = record.pending_flush_task
        record.pending_flush_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    async def _notify(record: ExecutionRecord) -> None:
        async with record.condition:
            record.condition.notify_all()

    @staticmethod
    def _raise_pending_flush_error(record: ExecutionRecord) -> None:
        if record.pending_flush_error is not None:
            raise RuntimeError("SSE 事件后台持久化失败") from record.pending_flush_error

    async def subscribe(
        self, record: ExecutionRecord, after_sequence: int = -1
    ) -> AsyncIterator[SSEEvent]:
        next_sequence = after_sequence + 1
        while True:
            async with record.condition:
                await record.condition.wait_for(
                    lambda: next_sequence < len(record.events)
                    or record.status in TERMINAL_STATUSES
                    or record.pending_flush_error is not None
                )
                _raise_flush_error(record)
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
                or record.pending_flush_error is not None
            )
            _raise_flush_error(record)
            assert record.response is not None
            return record.response

    def _cache(self, record: ExecutionRecord) -> None:
        self._by_request[(record.tenant_id, record.request_id)] = record
        self._by_response[(record.tenant_id, record.response_id)] = record

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite execution store 尚未初始化或已经关闭")
        return self._connection

    def _initialize_sync(self) -> None:
        if self._connection is not None:
            raise RuntimeError("SQLite execution store 不得重复初始化")
        connection = self._connect()
        self._connection = connection
        try:
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
        except BaseException:
            connection.rollback()
            connection.close()
            self._connection = None
            raise

    def _close_connection_sync(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def _recover_interrupted_sync(self) -> None:
        connection = self._require_connection()
        try:
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
        except BaseException:
            connection.rollback()
            raise

    def _create_or_get_sync(
        self, record: ExecutionRecord
    ) -> tuple[sqlite3.Row, bool]:
        now = time.time()
        connection = self._require_connection()
        try:
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
        except BaseException:
            connection.rollback()
            raise

    def _save_sync(self, record: ExecutionRecord) -> None:
        connection = self._require_connection()
        try:
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
        except BaseException:
            connection.rollback()
            raise

    def _append_events_sync(
        self,
        record: ExecutionRecord,
        events: list[SSEEvent],
        persist_record: bool = False,
    ) -> None:
        if not events:
            return
        connection = self._require_connection()
        try:
            expected = record.persisted_sequence + 1
            if events[0].sequence != expected:
                raise RuntimeError(
                    f"持久化 SSE sequence 应为 {expected}，实际为 {events[0].sequence}"
                )
            for offset, event in enumerate(events):
                if event.sequence != expected + offset:
                    raise RuntimeError(
                        "批量持久化 SSE sequence 不连续："
                        f"应为 {expected + offset}，实际为 {event.sequence}"
                    )
            now = time.time()
            connection.executemany(
                """INSERT INTO execution_events
                   (tenant_id, response_id, sequence, event_id, event_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        record.tenant_id,
                        record.response_id,
                        event.sequence,
                        event.event_id,
                        event.model_dump_json(exclude_none=True),
                        now,
                    )
                    for event in events
                ],
            )
            terminal = events[-1] if _terminal_event(events[-1]) else None
            if terminal is not None and terminal.response is not None:
                connection.execute(
                    """UPDATE executions
                       SET status = ?, provider_response_id = ?,
                           response_json = ?, updated_at = ?
                    WHERE tenant_id = ? AND response_id = ?""",
                    (
                        terminal.response.status,
                        terminal.response.provider_response_id,
                        terminal.response.model_dump_json(exclude_none=True),
                        now,
                        record.tenant_id,
                        record.response_id,
                    ),
                )
            elif persist_record:
                connection.execute(
                    """UPDATE executions
                       SET status = ?, provider_response_id = ?, response_json = ?,
                           updated_at = ?
                       WHERE tenant_id = ? AND request_id = ?""",
                    (
                        record.status.value,
                        record.provider_response_id,
                        self._response_json(record.response),
                        now,
                        record.tenant_id,
                        record.request_id,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _append_event_sync(self, record: ExecutionRecord, event: SSEEvent) -> None:
        """Compatibility wrapper for focused tests and external diagnostics."""
        self._append_events_sync(record, [event])

    def _hydrate_sync(self, row: sqlite3.Row) -> ExecutionRecord:
        response = (
            KemoResponse.model_validate_json(row["response_json"])
            if row["response_json"]
            else None
        )
        connection = self._require_connection()
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
            persisted_sequence=(events[-1].sequence if events else -1),
        )

    def _select_one_sync(
        self, query: str, parameters: tuple[str, str]
    ) -> sqlite3.Row | None:
        return self._require_connection().execute(query, parameters).fetchone()

    def _cleanup_expired_sync(self, cutoff: float) -> list[tuple[str, str]]:
        connection = self._require_connection()
        try:
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
        except BaseException:
            connection.rollback()
            raise
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


def _apply_terminal_event(record: ExecutionRecord, event: SSEEvent) -> None:
    """Publish terminal state in memory only after its event is durable."""
    if not _terminal_event(event) or event.response is None:
        return
    record.response = event.response
    record.provider_response_id = event.response.provider_response_id
    record.status = InternalStatus(event.response.status)


def _raise_flush_error(record: ExecutionRecord) -> None:
    if record.pending_flush_error is not None:
        raise RuntimeError("SSE 事件后台持久化失败") from record.pending_flush_error
