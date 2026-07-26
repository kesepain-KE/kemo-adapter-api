"""进程级运行状态；用于 Drain、活动执行计数和重启协调。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class GatewayPhase(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPING = "stopping"


class GatewayDrainingError(RuntimeError):
    pass


@dataclass(slots=True)
class ExecutionLease:
    state: "GatewayRuntimeState"
    released: bool = False

    async def release(self) -> None:
        if not self.released:
            self.released = True
            await self.state._release_execution()


class GatewayRuntimeState:
    def __init__(self) -> None:
        self.instance_id = f"instance_{uuid4().hex}"
        self.started_at = datetime.now(UTC)
        self.phase = GatewayPhase.STARTING
        self.active_executions = 0
        self.drain_reason: str | None = None
        self.drain_requested_by: str | None = None
        self._condition = asyncio.Condition()

    async def mark_running(self) -> None:
        async with self._condition:
            self.phase = GatewayPhase.RUNNING
            self.drain_reason = None
            self.drain_requested_by = None
            self._condition.notify_all()

    async def begin_drain(self, *, reason: str, requested_by: str) -> None:
        async with self._condition:
            if self.phase == GatewayPhase.STOPPING:
                raise RuntimeError("网关已经进入停止阶段")
            self.phase = GatewayPhase.DRAINING
            self.drain_reason = reason
            self.drain_requested_by = requested_by
            self._condition.notify_all()

    async def cancel_drain(self) -> None:
        async with self._condition:
            if self.phase == GatewayPhase.DRAINING:
                self.phase = GatewayPhase.RUNNING
                self.drain_reason = None
                self.drain_requested_by = None
                self._condition.notify_all()

    async def mark_stopping(self) -> None:
        async with self._condition:
            self.phase = GatewayPhase.STOPPING
            self._condition.notify_all()

    async def admit_execution(self) -> ExecutionLease:
        async with self._condition:
            if self.phase != GatewayPhase.RUNNING:
                raise GatewayDrainingError("网关正在排空或重启，暂不接受新的模型请求")
            self.active_executions += 1
            return ExecutionLease(self)

    async def _release_execution(self) -> None:
        async with self._condition:
            self.active_executions = max(0, self.active_executions - 1)
            self._condition.notify_all()

    async def wait_for_idle(self, timeout_seconds: float) -> bool:
        async def wait() -> None:
            async with self._condition:
                await self._condition.wait_for(lambda: self.active_executions == 0)

        try:
            await asyncio.wait_for(wait(), timeout=max(0.01, timeout_seconds))
            return True
        except TimeoutError:
            return False

    def snapshot(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "phase": self.phase.value,
            "active_executions": self.active_executions,
            "started_at": self.started_at.isoformat(),
            "drain_reason": self.drain_reason,
        }
