"""重启请求、状态、锁和实例元数据的原子文件协议。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class RestartPaths:
    root: Path

    @property
    def directory(self) -> Path:
        return self.root / "core" / "runtime"

    @property
    def pid(self) -> Path:
        return self.directory / "gateway.pid.json"

    @property
    def request(self) -> Path:
        return self.directory / "restart-request.json"

    @property
    def status(self) -> Path:
        return self.directory / "restart-status.json"

    @property
    def lock(self) -> Path:
        return self.directory / "restart.lock"


@dataclass(frozen=True, slots=True)
class RestartRequest:
    request_id: str
    reason: str
    force: bool
    requested_by: str
    requested_at: str
    drain_timeout_seconds: float
    startup_timeout_seconds: float

    @classmethod
    def create(
        cls,
        *,
        reason: str,
        force: bool,
        requested_by: str,
        drain_timeout_seconds: float,
        startup_timeout_seconds: float,
    ) -> "RestartRequest":
        normalized_reason = " ".join(reason.split())[:500] or "manual restart"
        return cls(
            request_id=f"restart_{uuid4().hex}",
            reason=normalized_reason,
            force=force,
            requested_by=requested_by[:200],
            requested_at=utc_now(),
            drain_timeout_seconds=drain_timeout_seconds,
            startup_timeout_seconds=startup_timeout_seconds,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RestartRequest":
        return cls(
            request_id=str(value["request_id"]),
            reason=str(value["reason"]),
            force=bool(value["force"]),
            requested_by=str(value["requested_by"]),
            requested_at=str(value["requested_at"]),
            drain_timeout_seconds=float(value["drain_timeout_seconds"]),
            startup_timeout_seconds=float(value["startup_timeout_seconds"]),
        )


class RestartAlreadyRunning(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > 64 * 1024:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 的 os.kill(pid, 0) 可能映射到 TerminateProcess，不能用于只读探测。
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def terminate_process(pid: int, *, force: bool = False) -> bool:
    """Terminate exactly one process identified by ``pid``.

    This is used only by the replacement process after the old gateway has
    already entered shutdown and exceeded its graceful-stop deadline.  It is
    deliberately kept here so Windows and POSIX use the same narrow API.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(process_terminate, False, pid)
        if not handle:
            return not process_exists(pid)
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    try:
        import signal

        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError):
        return not process_exists(pid)


def acquire_restart_lock(paths: RestartPaths, request: RestartRequest, gateway_pid: int) -> None:
    paths.directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(paths.lock, flags)
    except FileExistsError as exc:
        current = read_json(paths.lock) or {}
        locked_pid = int(current.get("gateway_pid", -1))
        if process_exists(locked_pid):
            raise RestartAlreadyRunning("已有重启任务正在执行") from exc
        paths.lock.unlink(missing_ok=True)
        try:
            descriptor = os.open(paths.lock, flags)
        except FileExistsError as retry_exc:
            raise RestartAlreadyRunning("已有重启任务正在执行") from retry_exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "request_id": request.request_id,
                "gateway_pid": gateway_pid,
                "created_at": utc_now(),
            },
            handle,
            ensure_ascii=False,
        )


def submit_restart(paths: RestartPaths, request: RestartRequest, gateway_pid: int) -> None:
    acquire_restart_lock(paths, request, gateway_pid)
    try:
        atomic_json(paths.request, asdict(request))
        write_restart_status(
            paths,
            request_id=request.request_id,
            phase="queued",
            message="重启请求已排队",
        )
    except Exception:
        paths.lock.unlink(missing_ok=True)
        raise


def read_restart_request(paths: RestartPaths) -> RestartRequest | None:
    value = read_json(paths.request)
    if value is None:
        return None
    try:
        return RestartRequest.from_dict(value)
    except (KeyError, TypeError, ValueError):
        return None


def write_restart_status(
    paths: RestartPaths,
    *,
    request_id: str,
    phase: str,
    message: str,
    active_executions: int | None = None,
    new_instance_id: str | None = None,
) -> None:
    atomic_json(
        paths.status,
        {
            "request_id": request_id,
            "phase": phase,
            "message": message[:500],
            "updated_at": utc_now(),
            "active_executions": active_executions,
            "new_instance_id": new_instance_id,
        },
    )


def release_restart(paths: RestartPaths, *, remove_request: bool = True) -> None:
    if remove_request:
        paths.request.unlink(missing_ok=True)
    paths.lock.unlink(missing_ok=True)


def write_pid_metadata(
    paths: RestartPaths,
    *,
    pid: int,
    instance_id: str,
    host: str,
    port: int,
    environment_override_names: list[str],
    dotenv_names: list[str] | None = None,
) -> None:
    atomic_json(
        paths.pid,
        {
            "pid": pid,
            "instance_id": instance_id,
            "project_root": str(paths.root.resolve()),
            "host": host,
            "port": port,
            "started_at": utc_now(),
            "environment_override_names": sorted(set(environment_override_names)),
            "dotenv_names": sorted(set(dotenv_names or [])),
        },
    )


def clear_pid_metadata(paths: RestartPaths, instance_id: str) -> None:
    current = read_json(paths.pid)
    if current and current.get("instance_id") == instance_id:
        paths.pid.unlink(missing_ok=True)
