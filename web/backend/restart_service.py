"""运行中网关的 Drain 与进程替换协调。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from core.restart_control import (
    RestartAlreadyRunning,
    RestartPaths,
    RestartRequest,
    read_json,
    read_restart_request,
    submit_restart,
    write_restart_status,
)
from core.runtime_state import GatewayRuntimeState


class RestartService:
    def __init__(self, project_root: Path, runtime_state: GatewayRuntimeState) -> None:
        self.project_root = project_root.resolve()
        self.paths = RestartPaths(self.project_root)
        self.runtime_state = runtime_state
        self.server: Any | None = None
        self._processing_request_id: str | None = None
        self._watcher_task: asyncio.Task[None] | None = None

    def configure_server(self, server: Any) -> None:
        self.server = server

    @staticmethod
    def _timeout(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError:
            return default
        return min(max(value, 1.0), 3600.0)

    def enqueue(self, *, reason: str, force: bool, requested_by: str) -> RestartRequest:
        request = RestartRequest.create(
            reason=reason,
            force=force,
            requested_by=requested_by,
            drain_timeout_seconds=self._timeout("RESTART_DRAIN_TIMEOUT", 120.0),
            startup_timeout_seconds=self._timeout("RESTART_STARTUP_TIMEOUT", 60.0),
        )
        submit_restart(self.paths, request, os.getpid())
        return request

    def status(self) -> dict[str, Any]:
        return read_json(self.paths.status) or {
            "request_id": None,
            "phase": "idle",
            "message": "当前没有重启任务",
            "updated_at": None,
            "active_executions": self.runtime_state.active_executions,
            "new_instance_id": None,
        }

    async def start_watcher(self) -> None:
        if self._watcher_task is None or self._watcher_task.done():
            self._watcher_task = asyncio.create_task(
                self._watch_requests(), name="gateway-restart-watcher"
            )

    async def stop_watcher(self) -> None:
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass

    async def _watch_requests(self) -> None:
        while True:
            request = read_restart_request(self.paths)
            if request and request.request_id != self._processing_request_id:
                self._processing_request_id = request.request_id
                await self._process(request)
            await asyncio.sleep(0.4)

    async def _process(self, request: RestartRequest) -> None:
        from restart import preflight

        if self.server is None:
            write_restart_status(
                self.paths,
                request_id=request.request_id,
                phase="failed",
                message="当前启动方式不支持进程重启，请使用 start_web.py",
            )
            self.paths.request.unlink(missing_ok=True)
            self.paths.lock.unlink(missing_ok=True)
            return
        if not preflight(self.project_root):
            write_restart_status(
                self.paths,
                request_id=request.request_id,
                phase="failed",
                message="启动前检查失败，旧网关保持运行",
            )
            self.paths.request.unlink(missing_ok=True)
            self.paths.lock.unlink(missing_ok=True)
            return

        write_restart_status(
            self.paths,
            request_id=request.request_id,
            phase="draining",
            message="正在等待活动请求结束",
            active_executions=self.runtime_state.active_executions,
        )
        await self.runtime_state.begin_drain(
            reason=request.reason, requested_by=request.requested_by
        )
        idle = await self.runtime_state.wait_for_idle(request.drain_timeout_seconds)
        if not idle and not request.force:
            await self.runtime_state.cancel_drain()
            write_restart_status(
                self.paths,
                request_id=request.request_id,
                phase="failed",
                message="Drain 超时，已取消重启并恢复接收新请求",
                active_executions=self.runtime_state.active_executions,
            )
            self.paths.request.unlink(missing_ok=True)
            self.paths.lock.unlink(missing_ok=True)
            return

        write_restart_status(
            self.paths,
            request_id=request.request_id,
            phase="stopping",
            message=("强制重启，活动请求可能中断" if not idle else "旧实例正在优雅停止"),
            active_executions=self.runtime_state.active_executions,
        )
        command = [
            sys.executable,
            str(self.project_root / "restart.py"),
            "--replace",
            "--request-id",
            request.request_id,
            "--old-pid",
            str(os.getpid()),
            "--old-instance-id",
            self.runtime_state.instance_id,
            "--startup-timeout",
            str(request.startup_timeout_seconds),
        ]
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
                | subprocess.DETACHED_PROCESS
            )
        else:
            start_new_session = True
        try:
            subprocess.Popen(
                command,
                cwd=self.project_root,
                close_fds=True,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except OSError:
            await self.runtime_state.cancel_drain()
            write_restart_status(
                self.paths,
                request_id=request.request_id,
                phase="failed",
                message="无法启动替换进程，旧网关保持运行",
            )
            self.paths.request.unlink(missing_ok=True)
            self.paths.lock.unlink(missing_ok=True)
            return

        await self.runtime_state.mark_stopping()
        # Tell uvicorn to leave its serve loop immediately.  The detached
        # replacement process independently waits for this PID and its port
        # to disappear before loading the new .env and starting.
        self.server.should_exit = True


__all__ = ["RestartAlreadyRunning", "RestartService"]
