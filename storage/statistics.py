"""Daily SQLite statistics for normalized gateway invocations.

This module deliberately knows nothing about provider-specific token fields.
It only consumes :class:`core.models.Usage` after a provider adapter has
normalized its accounting semantics.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from core.models import Usage


DIMENSIONS = frozenset({"provider", "model", "gateway_key"})
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "visible_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True, slots=True)
class InvocationHandle:
    invocation_id: str
    day: str
    database: Path
    started_perf_ns: int
    task: str
    provider_id: str
    model: str
    gateway_key_id: str | None


class StatisticsStore:
    """Serialized async facade over per-day SQLite databases.

    All write methods are best-effort. A storage outage must never change a
    model response, so failures are reflected by ``health()`` instead of being
    raised into the execution path.
    """

    def __init__(self, root: Path, *, timezone_name: str = "Asia/Shanghai") -> None:
        self.root = root.resolve()
        self.daily_root = self.root / "daily"
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)
        self._lock = asyncio.Lock()
        self._healthy = True
        self._dropped_events = 0
        self._last_error: str | None = None
        self._initialized_paths: set[Path] = set()

    async def initialize(self) -> None:
        """Create today's database and validate the configured timezone."""
        day = datetime.now(self.timezone).date().isoformat()
        try:
            async with self._lock:
                await asyncio.to_thread(self._initialize_database, self._path_for_day(day))
            self._mark_success()
        except Exception as exc:
            self._mark_failure(exc)

    def health(self) -> dict[str, object]:
        return {
            "healthy": self._healthy,
            "dropped_events": self._dropped_events,
            "last_error": self._last_error,
            "timezone": self.timezone_name,
        }

    async def begin_invocation(
        self,
        *,
        task: str,
        provider_id: str,
        model: str,
        tenant_id: str,
        gateway_key_id: str | None,
        request_id: str,
        response_id: str | None = None,
    ) -> InvocationHandle | None:
        now_utc = datetime.now(timezone.utc)
        day = now_utc.astimezone(self.timezone).date().isoformat()
        handle = InvocationHandle(
            invocation_id=f"inv_{uuid4().hex}",
            day=day,
            database=self._path_for_day(day),
            started_perf_ns=time.perf_counter_ns(),
            task=task,
            provider_id=provider_id,
            model=model,
            gateway_key_id=gateway_key_id,
        )
        try:
            async with self._lock:
                await asyncio.to_thread(
                    self._begin_sync,
                    handle,
                    now_utc.isoformat(),
                    tenant_id,
                    request_id,
                    response_id,
                )
            self._mark_success()
            return handle
        except Exception as exc:  # statistics must remain out of the response path
            self._mark_failure(exc)
            return None

    async def finish_invocation(
        self,
        handle: InvocationHandle | None,
        *,
        status: str,
        usage: Usage | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        provider_response_id: str | None = None,
    ) -> None:
        if handle is None:
            return
        latency_ms = max(0.0, (time.perf_counter_ns() - handle.started_perf_ns) / 1_000_000)
        try:
            async with self._lock:
                await asyncio.to_thread(
                    self._finish_sync,
                    handle,
                    datetime.now(timezone.utc).isoformat(),
                    status,
                    usage,
                    error_code,
                    error_type,
                    error_message,
                    provider_response_id,
                    latency_ms,
                )
            self._mark_success()
        except Exception as exc:
            self._mark_failure(exc)

    async def record_replay(
        self,
        *,
        task: str,
        provider_id: str,
        model: str,
        gateway_key_id: str | None,
    ) -> None:
        day = datetime.now(self.timezone).date().isoformat()
        try:
            async with self._lock:
                await asyncio.to_thread(
                    self._record_replay_sync,
                    self._path_for_day(day),
                    day,
                    task,
                    provider_id,
                    model,
                    gateway_key_id,
                )
            self._mark_success()
        except Exception as exc:
            self._mark_failure(exc)

    async def daily(self, value: str | date) -> dict[str, object]:
        day = self._normalize_day(value)
        async with self._lock:
            return await asyncio.to_thread(self._daily_sync, day)

    async def rankings(
        self, value: str | date, dimension: str, *, limit: int = 20
    ) -> dict[str, object]:
        day = self._normalize_day(value)
        if dimension not in DIMENSIONS:
            raise ValueError(f"unsupported ranking dimension: {dimension}")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._lock:
            return await asyncio.to_thread(self._rankings_sync, day, dimension, limit)

    async def hourly(self, value: str | date) -> dict[str, object]:
        day = self._normalize_day(value)
        async with self._lock:
            return await asyncio.to_thread(self._hourly_sync, day)

    async def series(self, start: str | date, end: str | date) -> dict[str, object]:
        start_day = self._normalize_day(start)
        end_day = self._normalize_day(end)
        if end_day < start_day:
            raise ValueError("end date must not precede start date")
        if (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days > 366:
            raise ValueError("date range must not exceed 366 days")
        async with self._lock:
            return await asyncio.to_thread(self._series_sync, start_day, end_day)

    async def gateway_key_usage(self) -> dict[str, dict[str, object]]:
        """Aggregate real all-time usage for every recorded gateway key id."""
        async with self._lock:
            return await asyncio.to_thread(self._gateway_key_usage_sync)

    async def recent_invocations(
        self,
        outcome: str = "all",
        *,
        limit: int = 50,
        day: str | None = None,
        hour: int | None = None,
        offset: int = 0,
    ) -> dict[str, object]:
        """Return one page from the unbounded secret-safe invocation history."""
        if outcome not in {"all", "success", "failure"}:
            raise ValueError("outcome must be all, success or failure")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        if hour is not None and not 0 <= hour <= 23:
            raise ValueError("hour must be between 0 and 23")
        if day is not None:
            self._path_for_day(day)
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_invocations_sync,
                outcome,
                limit,
                day,
                hour,
                offset,
            )

    def _path_for_day(self, day: str) -> Path:
        parsed = date.fromisoformat(day)
        return self.daily_root / f"{parsed.year:04d}" / f"{parsed.month:02d}" / f"{day}.sqlite3"

    @staticmethod
    def _normalize_day(value: str | date) -> str:
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(value).isoformat()

    def _connect(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_database(self, path: Path) -> None:
        with self._connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    task TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    gateway_key_id TEXT,
                    request_id TEXT NOT NULL,
                    response_id TEXT,
                    provider_response_id TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    latency_ms REAL,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    output_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    visible_output_tokens INTEGER,
                    total_tokens INTEGER,
                    usage_mode TEXT,
                    usage_exact INTEGER,
                    usage_exact_fields TEXT,
                    usage_estimated_fields TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_invocations_day_status
                    ON invocations(day, status);
                CREATE INDEX IF NOT EXISTS idx_invocations_request
                    ON invocations(tenant_id, task, request_id);
                CREATE INDEX IF NOT EXISTS idx_invocations_started_at
                    ON invocations(started_at DESC);

                CREATE TABLE IF NOT EXISTS daily_rollups (
                    day TEXT NOT NULL,
                    task TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    dimension_key TEXT NOT NULL,
                    calls INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    cancellations INTEGER NOT NULL DEFAULT 0,
                    incompletes INTEGER NOT NULL DEFAULT 0,
                    running INTEGER NOT NULL DEFAULT 0,
                    replay_count INTEGER NOT NULL DEFAULT 0,
                    latency_sum_ms REAL NOT NULL DEFAULT 0,
                    latency_samples INTEGER NOT NULL DEFAULT 0,
                    input_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    input_tokens_samples INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens_samples INTEGER NOT NULL DEFAULT 0,
                    output_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    output_tokens_samples INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens_samples INTEGER NOT NULL DEFAULT 0,
                    visible_output_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    visible_output_tokens_samples INTEGER NOT NULL DEFAULT 0,
                    total_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    total_tokens_samples INTEGER NOT NULL DEFAULT 0,
                    cache_eligible_input_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    cache_eligible_cached_tokens_sum INTEGER NOT NULL DEFAULT 0,
                    cache_eligible_samples INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, task, dimension, dimension_key)
                );
                PRAGMA user_version=1;
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(invocations)")
            }
            if "error_type" not in columns:
                connection.execute("ALTER TABLE invocations ADD COLUMN error_type TEXT")
            if "error_message" not in columns:
                connection.execute("ALTER TABLE invocations ADD COLUMN error_message TEXT")
            connection.execute("PRAGMA user_version=2")
        self._initialized_paths.add(path)

    def _ensure_database(self, path: Path) -> None:
        if path not in self._initialized_paths:
            self._initialize_database(path)

    @staticmethod
    def _dimensions(provider_id: str, model: str, gateway_key_id: str | None):
        values = [("all", "all"), ("provider", provider_id), ("model", model)]
        if gateway_key_id:
            values.append(("gateway_key", gateway_key_id))
        return values

    def _ensure_rollup(
        self, connection: sqlite3.Connection, day: str, task: str, dimension: str, key: str
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO daily_rollups(day, task, dimension, dimension_key) VALUES(?,?,?,?)",
            (day, task, dimension, key),
        )

    def _begin_sync(
        self,
        handle: InvocationHandle,
        started_at: str,
        tenant_id: str,
        request_id: str,
        response_id: str | None,
    ) -> None:
        self._ensure_database(handle.database)
        with self._connect(handle.database) as connection:
            connection.execute(
                """INSERT INTO invocations(
                    invocation_id, day, task, provider_id, model, tenant_id,
                    gateway_key_id, request_id, response_id, started_at, status
                ) VALUES(?,?,?,?,?,?,?,?,?,?, 'running')""",
                (
                    handle.invocation_id,
                    handle.day,
                    handle.task,
                    handle.provider_id,
                    handle.model,
                    tenant_id,
                    handle.gateway_key_id,
                    request_id,
                    response_id,
                    started_at,
                ),
            )
            for dimension, key in self._dimensions(
                handle.provider_id, handle.model, handle.gateway_key_id
            ):
                self._ensure_rollup(connection, handle.day, handle.task, dimension, key)
                connection.execute(
                    """UPDATE daily_rollups SET calls=calls+1, running=running+1
                       WHERE day=? AND task=? AND dimension=? AND dimension_key=?""",
                    (handle.day, handle.task, dimension, key),
                )

    def _finish_sync(
        self,
        handle: InvocationHandle,
        finished_at: str,
        status: str,
        usage: Usage | None,
        error_code: str | None,
        error_type: str | None,
        error_message: str | None,
        provider_response_id: str | None,
        latency_ms: float,
    ) -> None:
        normalized = self._usage_values(usage)
        with self._connect(handle.database) as connection:
            row = connection.execute(
                "SELECT status FROM invocations WHERE invocation_id=?", (handle.invocation_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            connection.execute(
                """UPDATE invocations SET finished_at=?, status=?, error_code=?,
                    error_type=?, error_message=?,
                    provider_response_id=?, latency_ms=?, input_tokens=?,
                    cached_input_tokens=?, output_tokens=?, reasoning_tokens=?,
                    visible_output_tokens=?, total_tokens=?, usage_mode=?, usage_exact=?,
                    usage_exact_fields=?, usage_estimated_fields=?
                   WHERE invocation_id=?""",
                (
                    finished_at,
                    status,
                    error_code,
                    str(error_type or "")[:160] or None,
                    str(error_message or "")[:2000] or None,
                    provider_response_id,
                    latency_ms,
                    *(normalized[field] for field in TOKEN_FIELDS),
                    normalized["usage_mode"],
                    normalized["usage_exact"],
                    normalized["usage_exact_fields"],
                    normalized["usage_estimated_fields"],
                    handle.invocation_id,
                ),
            )
            buckets = {
                "completed": "successes",
                "requires_action": "successes",
                "failed": "failures",
                "cancelled": "cancellations",
                "incomplete": "failures",
            }
            bucket = buckets.get(status, "failures")
            for dimension, key in self._dimensions(
                handle.provider_id, handle.model, handle.gateway_key_id
            ):
                assignments = ["running=MAX(0,running-1)", f"{bucket}={bucket}+1"]
                if status == "incomplete":
                    assignments.append("incompletes=incompletes+1")
                params: list[object] = []
                assignments.extend(["latency_sum_ms=latency_sum_ms+?", "latency_samples=latency_samples+1"])
                params.append(latency_ms)
                for field in TOKEN_FIELDS:
                    value = normalized[field]
                    if value is not None:
                        assignments.extend(
                            [f"{field}_sum={field}_sum+?", f"{field}_samples={field}_samples+1"]
                        )
                        params.append(value)
                if normalized["cache_eligible"]:
                    assignments.extend(
                        [
                            "cache_eligible_input_tokens_sum=cache_eligible_input_tokens_sum+?",
                            "cache_eligible_cached_tokens_sum=cache_eligible_cached_tokens_sum+?",
                            "cache_eligible_samples=cache_eligible_samples+1",
                        ]
                    )
                    params.extend([normalized["input_tokens"], normalized["cached_input_tokens"]])
                params.extend([handle.day, handle.task, dimension, key])
                connection.execute(
                    f"UPDATE daily_rollups SET {', '.join(assignments)} "
                    "WHERE day=? AND task=? AND dimension=? AND dimension_key=?",
                    params,
                )

    @staticmethod
    def _usage_values(usage: Usage | None) -> dict[str, Any]:
        values: dict[str, Any] = {field: None for field in TOKEN_FIELDS}
        values.update(
            usage_mode=None,
            usage_exact=None,
            usage_exact_fields="[]",
            usage_estimated_fields="[]",
            cache_eligible=False,
        )
        if usage is None:
            return values
        for field in TOKEN_FIELDS:
            value = getattr(usage, field)
            values[field] = int(value) if value is not None else None
        measurement = usage.measurement
        exact_fields = set(measurement.exact_fields)
        values.update(
            usage_mode=measurement.mode,
            usage_exact=int(measurement.exact),
            usage_exact_fields=json.dumps(sorted(exact_fields), ensure_ascii=False),
            usage_estimated_fields=json.dumps(measurement.estimated_fields, ensure_ascii=False),
        )
        exact_cache_pair = measurement.exact or {
            "input_tokens",
            "cached_input_tokens",
        }.issubset(exact_fields)
        values["cache_eligible"] = bool(
            exact_cache_pair
            and values["input_tokens"] is not None
            and values["cached_input_tokens"] is not None
        )
        return values

    def _record_replay_sync(
        self,
        path: Path,
        day: str,
        task: str,
        provider_id: str,
        model: str,
        gateway_key_id: str | None,
    ) -> None:
        self._ensure_database(path)
        with self._connect(path) as connection:
            for dimension, key in self._dimensions(provider_id, model, gateway_key_id):
                self._ensure_rollup(connection, day, task, dimension, key)
                connection.execute(
                    """UPDATE daily_rollups SET replay_count=replay_count+1
                       WHERE day=? AND task=? AND dimension=? AND dimension_key=?""",
                    (day, task, dimension, key),
                )

    @staticmethod
    def _empty_metrics() -> dict[str, object]:
        return {
            "calls": 0,
            "successes": 0,
            "failures": 0,
            "cancellations": 0,
            "incompletes": 0,
            "running": 0,
            "replay_count": 0,
            "success_rate": None,
            "average_latency_ms": None,
            "tokens": {field: None for field in TOKEN_FIELDS},
            "token_coverage": {field: 0 for field in TOKEN_FIELDS},
            "cache_hit_rate": None,
            "cache_eligible_samples": 0,
        }

    def _row_metrics(self, row: sqlite3.Row | None) -> dict[str, object]:
        if row is None or row["calls"] is None:
            return self._empty_metrics()
        terminal = int(row["successes"]) + int(row["failures"]) + int(row["cancellations"])
        metrics = {
            key: int(row[key])
            for key in (
                "calls", "successes", "failures", "cancellations", "incompletes", "running", "replay_count"
            )
        }
        metrics["success_rate"] = (int(row["successes"]) / terminal) if terminal else None
        latency_samples = int(row["latency_samples"])
        metrics["average_latency_ms"] = (
            float(row["latency_sum_ms"]) / latency_samples if latency_samples else None
        )
        metrics["tokens"] = {
            field: int(row[f"{field}_sum"]) if int(row[f"{field}_samples"]) else None
            for field in TOKEN_FIELDS
        }
        metrics["token_coverage"] = {
            field: int(row[f"{field}_samples"]) for field in TOKEN_FIELDS
        }
        eligible_input = int(row["cache_eligible_input_tokens_sum"])
        eligible_samples = int(row["cache_eligible_samples"])
        metrics["cache_hit_rate"] = (
            int(row["cache_eligible_cached_tokens_sum"]) / eligible_input
            if eligible_samples and eligible_input > 0
            else None
        )
        metrics["cache_eligible_samples"] = eligible_samples
        return metrics

    def _read_rollup(self, day: str, dimension: str, key: str, task: str = "all"):
        path = self._path_for_day(day)
        if not path.exists():
            return None
        with self._connect(path) as connection:
            if task == "all":
                return connection.execute(
                    """SELECT
                        SUM(calls) calls, SUM(successes) successes, SUM(failures) failures,
                        SUM(cancellations) cancellations, SUM(incompletes) incompletes,
                        SUM(running) running, SUM(replay_count) replay_count,
                        SUM(latency_sum_ms) latency_sum_ms, SUM(latency_samples) latency_samples,
                        SUM(input_tokens_sum) input_tokens_sum, SUM(input_tokens_samples) input_tokens_samples,
                        SUM(cached_input_tokens_sum) cached_input_tokens_sum, SUM(cached_input_tokens_samples) cached_input_tokens_samples,
                        SUM(output_tokens_sum) output_tokens_sum, SUM(output_tokens_samples) output_tokens_samples,
                        SUM(reasoning_tokens_sum) reasoning_tokens_sum, SUM(reasoning_tokens_samples) reasoning_tokens_samples,
                        SUM(visible_output_tokens_sum) visible_output_tokens_sum, SUM(visible_output_tokens_samples) visible_output_tokens_samples,
                        SUM(total_tokens_sum) total_tokens_sum, SUM(total_tokens_samples) total_tokens_samples,
                        SUM(cache_eligible_input_tokens_sum) cache_eligible_input_tokens_sum,
                        SUM(cache_eligible_cached_tokens_sum) cache_eligible_cached_tokens_sum,
                        SUM(cache_eligible_samples) cache_eligible_samples
                       FROM daily_rollups WHERE day=? AND dimension=? AND dimension_key=?""",
                    (day, dimension, key),
                ).fetchone()
            return connection.execute(
                "SELECT * FROM daily_rollups WHERE day=? AND task=? AND dimension=? AND dimension_key=?",
                (day, task, dimension, key),
            ).fetchone()

    def _daily_sync(self, day: str) -> dict[str, object]:
        row = self._read_rollup(day, "all", "all")
        return {"date": day, "timezone": self.timezone_name, **self._row_metrics(row), "storage": self.health()}

    def _hourly_sync(self, day: str) -> dict[str, object]:
        buckets = [
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "terminal_calls": 0,
                "success_rate": None,
                "input_tokens": 0,
                "input_token_samples": 0,
                "cached_input_tokens": 0,
                "cached_input_token_samples": 0,
                "output_tokens": 0,
                "output_token_samples": 0,
                "total_tokens": 0,
                "token_samples": 0,
                "cache_eligible_input_tokens": 0,
                "cache_eligible_cached_tokens": 0,
                "cache_eligible_samples": 0,
                "cache_hit_rate": None,
            }
            for hour in range(24)
        ]
        path = self._path_for_day(day)
        if path.exists():
            with self._connect(path) as connection:
                rows = connection.execute(
                    """SELECT started_at, status, input_tokens, cached_input_tokens,
                              output_tokens, total_tokens, usage_exact, usage_exact_fields
                         FROM invocations WHERE day=?""",
                    (day,),
                ).fetchall()
            for row in rows:
                try:
                    started_at = datetime.fromisoformat(str(row["started_at"]))
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    hour = started_at.astimezone(self.timezone).hour
                except (TypeError, ValueError):
                    continue
                bucket = buckets[hour]
                bucket["calls"] = int(bucket["calls"]) + 1
                if row["status"] in {"completed", "requires_action"}:
                    bucket["successes"] = int(bucket["successes"]) + 1
                elif row["status"] in {"failed", "incomplete"}:
                    bucket["failures"] = int(bucket["failures"]) + 1
                if row["status"] in {
                    "completed",
                    "requires_action",
                    "failed",
                    "incomplete",
                    "cancelled",
                }:
                    bucket["terminal_calls"] = int(bucket["terminal_calls"]) + 1
                for field, sample_field in (
                    ("input_tokens", "input_token_samples"),
                    ("cached_input_tokens", "cached_input_token_samples"),
                    ("output_tokens", "output_token_samples"),
                    ("total_tokens", "token_samples"),
                ):
                    if row[field] is not None:
                        bucket[field] = int(bucket[field]) + int(row[field])
                        bucket[sample_field] = int(bucket[sample_field]) + 1
                try:
                    exact_fields = set(json.loads(row["usage_exact_fields"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    exact_fields = set()
                cache_eligible = bool(row["usage_exact"]) or {
                    "input_tokens",
                    "cached_input_tokens",
                }.issubset(exact_fields)
                if (
                    cache_eligible
                    and row["input_tokens"] is not None
                    and row["cached_input_tokens"] is not None
                ):
                    bucket["cache_eligible_input_tokens"] = int(
                        bucket["cache_eligible_input_tokens"]
                    ) + int(row["input_tokens"])
                    bucket["cache_eligible_cached_tokens"] = int(
                        bucket["cache_eligible_cached_tokens"]
                    ) + int(row["cached_input_tokens"])
                    bucket["cache_eligible_samples"] = int(
                        bucket["cache_eligible_samples"]
                    ) + 1
        for bucket in buckets:
            terminal_calls = int(bucket.pop("terminal_calls"))
            bucket["success_rate"] = (
                int(bucket["successes"]) / terminal_calls if terminal_calls else None
            )
            eligible_input = int(bucket.pop("cache_eligible_input_tokens"))
            eligible_cached = int(bucket.pop("cache_eligible_cached_tokens"))
            bucket["cache_hit_rate"] = (
                eligible_cached / eligible_input
                if int(bucket["cache_eligible_samples"]) and eligible_input > 0
                else None
            )
            # A bucket without calls is a real zero. A bucket with calls but no
            # accounting sample stays null so the UI can label it as unmeasured.
            if bucket["calls"]:
                for field, sample_field in (
                    ("input_tokens", "input_token_samples"),
                    ("cached_input_tokens", "cached_input_token_samples"),
                    ("output_tokens", "output_token_samples"),
                    ("total_tokens", "token_samples"),
                ):
                    if not bucket[sample_field]:
                        bucket[field] = None
        return {"date": day, "timezone": self.timezone_name, "items": buckets}

    def _rankings_sync(self, day: str, dimension: str, limit: int) -> dict[str, object]:
        path = self._path_for_day(day)
        if not path.exists():
            return {"date": day, "dimension": dimension, "items": []}
        with self._connect(path) as connection:
            rows = connection.execute(
                """SELECT dimension_key,
                    SUM(calls) calls, SUM(successes) successes, SUM(failures) failures,
                    SUM(cancellations) cancellations, SUM(incompletes) incompletes,
                    SUM(running) running, SUM(replay_count) replay_count,
                    SUM(latency_sum_ms) latency_sum_ms, SUM(latency_samples) latency_samples,
                    SUM(input_tokens_sum) input_tokens_sum, SUM(input_tokens_samples) input_tokens_samples,
                    SUM(cached_input_tokens_sum) cached_input_tokens_sum, SUM(cached_input_tokens_samples) cached_input_tokens_samples,
                    SUM(output_tokens_sum) output_tokens_sum, SUM(output_tokens_samples) output_tokens_samples,
                    SUM(reasoning_tokens_sum) reasoning_tokens_sum, SUM(reasoning_tokens_samples) reasoning_tokens_samples,
                    SUM(visible_output_tokens_sum) visible_output_tokens_sum, SUM(visible_output_tokens_samples) visible_output_tokens_samples,
                    SUM(total_tokens_sum) total_tokens_sum, SUM(total_tokens_samples) total_tokens_samples,
                    SUM(cache_eligible_input_tokens_sum) cache_eligible_input_tokens_sum,
                    SUM(cache_eligible_cached_tokens_sum) cache_eligible_cached_tokens_sum,
                    SUM(cache_eligible_samples) cache_eligible_samples
                   FROM daily_rollups WHERE day=? AND dimension=?
                   GROUP BY dimension_key ORDER BY calls DESC, dimension_key ASC LIMIT ?""",
                (day, dimension, limit),
            ).fetchall()
        return {
            "date": day,
            "dimension": dimension,
            "items": [
                {"id": row["dimension_key"], **self._row_metrics(row)} for row in rows
            ],
        }

    def _series_sync(self, start: str, end: str) -> dict[str, object]:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        items: list[dict[str, object]] = []
        aggregate_fields = (
            "calls",
            "successes",
            "failures",
            "cancellations",
            "incompletes",
            "running",
            "replay_count",
            "latency_sum_ms",
            "latency_samples",
            *(field for token_field in TOKEN_FIELDS for field in (
                f"{token_field}_sum",
                f"{token_field}_samples",
            )),
            "cache_eligible_input_tokens_sum",
            "cache_eligible_cached_tokens_sum",
            "cache_eligible_samples",
        )
        aggregate: dict[str, int | float] = {field: 0 for field in aggregate_fields}
        has_metrics = False
        current = start_date
        while current <= end_date:
            day = current.isoformat()
            row = self._read_rollup(day, "all", "all")
            items.append(
                {
                    "date": day,
                    "timezone": self.timezone_name,
                    **self._row_metrics(row),
                    "storage": self.health(),
                }
            )
            if row is not None and row["calls"] is not None:
                has_metrics = True
                for field in aggregate_fields:
                    aggregate[field] += row[field] or 0
            current = date.fromordinal(current.toordinal() + 1)
        return {
            "from": start,
            "to": end,
            "timezone": self.timezone_name,
            "summary": self._row_metrics(aggregate if has_metrics else None),
            "items": items,
        }

    def _gateway_key_usage_sync(self) -> dict[str, dict[str, object]]:
        aggregated: dict[str, dict[str, object]] = {}
        for path in sorted(self.daily_root.glob("*/*/*.sqlite3")):
            uri = f"file:{path.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT gateway_key_id,
                              COUNT(*) AS calls,
                              SUM(CASE WHEN status IN ('completed','requires_action') THEN 1 ELSE 0 END) AS successes,
                              SUM(CASE WHEN total_tokens IS NOT NULL THEN total_tokens ELSE 0 END) AS total_tokens,
                              SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END) AS token_samples,
                              MAX(started_at) AS last_used_at
                         FROM invocations
                        WHERE gateway_key_id IS NOT NULL AND gateway_key_id != ''
                        GROUP BY gateway_key_id"""
                ).fetchall()
            for row in rows:
                key_id = str(row["gateway_key_id"])
                current = aggregated.setdefault(
                    key_id,
                    {
                        "calls": 0,
                        "successes": 0,
                        "total_tokens": 0,
                        "token_samples": 0,
                        "last_used_at": None,
                    },
                )
                current["calls"] = int(current["calls"]) + int(row["calls"])
                current["successes"] = int(current["successes"]) + int(row["successes"])
                current["total_tokens"] = int(current["total_tokens"]) + int(row["total_tokens"])
                current["token_samples"] = int(current["token_samples"]) + int(row["token_samples"])
                last_used = row["last_used_at"]
                if last_used and (current["last_used_at"] is None or last_used > current["last_used_at"]):
                    current["last_used_at"] = str(last_used)
        for current in aggregated.values():
            if not current["token_samples"]:
                current["total_tokens"] = None
        return aggregated

    def _recent_invocations_sync(
        self,
        outcome: str,
        limit: int,
        day: str | None = None,
        hour: int | None = None,
        offset: int = 0,
    ) -> dict[str, object]:
        filters = {
            "all": ("", ()),
            "success": (" AND status IN ('completed','requires_action')", ()),
            "failure": (" AND status IN ('failed','incomplete')", ()),
        }
        predicate, parameters = filters[outcome]
        collected: list[dict[str, object]] = []
        total = 0
        paths = (
            [self._path_for_day(day)]
            if day is not None
            else sorted(self.daily_root.glob("*/*/*.sqlite3"), reverse=True)
        )
        for path in paths:
            time_predicate = ""
            time_parameters: tuple[object, ...] = ()
            if hour is not None:
                path_day = day or path.stem
                selected_day = date.fromisoformat(path_day)
                local_start = datetime(
                    selected_day.year,
                    selected_day.month,
                    selected_day.day,
                    hour,
                    tzinfo=self.timezone,
                )
                utc_start = local_start.astimezone(timezone.utc)
                utc_end = (local_start + timedelta(hours=1)).astimezone(timezone.utc)
                time_predicate = " AND started_at >= ? AND started_at < ?"
                time_parameters = (utc_start.isoformat(), utc_end.isoformat())
            uri = f"file:{path.as_posix()}?mode=ro"
            try:
                with sqlite3.connect(uri, uri=True, timeout=10) as connection:
                    connection.row_factory = sqlite3.Row
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(invocations)")
                    }
                    error_type_column = (
                        "error_type" if "error_type" in columns else "NULL AS error_type"
                    )
                    error_message_column = (
                        "error_message"
                        if "error_message" in columns
                        else "NULL AS error_message"
                    )
                    where = f"WHERE 1=1{predicate}{time_predicate}"
                    query_parameters = (*parameters, *time_parameters)
                    total += int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM invocations {where}",
                            query_parameters,
                        ).fetchone()[0]
                    )
                    rows = connection.execute(
                        f"""SELECT started_at, finished_at, task, provider_id, model,
                                   gateway_key_id, status, error_code,
                                   {error_type_column}, {error_message_column}, latency_ms,
                                   input_tokens, cached_input_tokens, output_tokens,
                                   reasoning_tokens, visible_output_tokens, total_tokens,
                                   usage_mode, usage_exact
                              FROM invocations
                             {where}
                             ORDER BY started_at DESC LIMIT ?""",
                        (*query_parameters, offset + limit),
                    ).fetchall()
            except (OSError, sqlite3.Error):
                continue
            collected.extend(
                {
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "task": row["task"],
                    "provider_id": row["provider_id"],
                    "model": row["model"],
                    "provider_model": (
                        str(row["model"])[len(str(row["provider_id"])) + 1:]
                        if str(row["model"]).startswith(f"{row['provider_id']}-")
                        else row["model"]
                    ),
                    "gateway_key_id": row["gateway_key_id"],
                    "status": row["status"],
                    "error_code": row["error_code"],
                    "error_type": row["error_type"],
                    "error_message": row["error_message"],
                    "latency_ms": row["latency_ms"],
                    "tokens": {field: row[field] for field in TOKEN_FIELDS},
                    "usage": {
                        "mode": row["usage_mode"],
                        "exact": bool(row["usage_exact"]) if row["usage_exact"] is not None else None,
                    },
                }
                for row in rows
            )
        collected.sort(key=lambda item: str(item["started_at"]), reverse=True)
        return {
            "outcome": outcome,
            "date": day,
            "hour": hour,
            "offset": offset,
            "limit": limit,
            "total": total,
            "items": collected[offset : offset + limit],
        }

    def _mark_success(self) -> None:
        self._healthy = True
        self._last_error = None

    def _mark_failure(self, exc: Exception) -> None:
        self._healthy = False
        self._dropped_events += 1
        self._last_error = type(exc).__name__
