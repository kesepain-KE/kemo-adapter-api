"""Kemo 网关平滑重启控制器。

普通调用会向运行中的 ``start_web.py`` 实例提交本地重启请求；``--replace`` 仅供旧实例
启动的独立替换进程使用。请求和状态文件不包含 API Key 或环境变量值。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values, load_dotenv

from core.restart_control import (
    RestartAlreadyRunning,
    RestartPaths,
    RestartRequest,
    process_exists,
    read_json,
    release_restart,
    terminate_process,
    submit_restart,
    write_restart_status,
)


PROJECT_ROOT = Path(__file__).resolve().parent
TERMINAL_PHASES = frozenset({"succeeded", "failed"})

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _timeout(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(max(value, 1.0), 3600.0)


@contextmanager
def _prospective_environment(project_root: Path) -> Iterator[None]:
    """临时模拟新进程环境，用于在停止旧实例前验证更新后的 .env。"""
    paths = RestartPaths(project_root)
    metadata = read_json(paths.pid) or {}
    override_names = set(metadata.get("environment_override_names", []))
    if not metadata:
        # 独立执行 ``restart.py --preflight`` 时没有 PID 元数据，应采用和
        # start_web.py 相同的“进程环境优先、.env 只补缺失值”语义。
        override_names = set(os.environ)
    managed_names = set(metadata.get("dotenv_names", []))
    managed_names.update(dotenv_values(project_root / ".env"))
    values = dotenv_values(project_root / ".env")
    previous: dict[str, str | None] = {}
    try:
        for name in managed_names:
            if name not in override_names:
                previous[name] = os.environ.get(name)
                os.environ.pop(name, None)
        for name, value in values.items():
            if name in override_names:
                continue
            # The first loop may already have saved and removed this value.
            # Keep that original snapshot so leaving preflight restores the
            # running gateway environment instead of replacing it with None.
            previous.setdefault(name, os.environ.get(name))
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _prospective_startup_options(project_root: Path) -> dict[str, object]:
    with _prospective_environment(project_root):
        from start_web import _startup_options

        return _startup_options()


def _open_health(host: str, port: int, *, host_header: str | None, timeout: float):
    request = Request(_health_url(host, port))
    if host_header:
        request.add_header("Host", host_header)
    return urlopen(request, timeout=timeout)


def _health_instance(
    host: str,
    port: int,
    *,
    host_header: str | None = None,
) -> str | None:
    try:
        with _open_health(
            host, port, host_header=host_header, timeout=1.0
        ) as response:
            body = json.loads(response.read(64 * 1024).decode("utf-8"))
        candidate = body.get("instance_id")
        if response.status == 200 and isinstance(candidate, str):
            return candidate
    except (OSError, URLError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def _isolated_startup_preflight(project_root: Path, metadata: dict) -> bool:
    """用新 Python 进程验证源码、依赖、应用装配和待生效环境。"""

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [sys.executable, str(project_root / "start_web.py"), "--preflight"],
            cwd=project_root,
            env=_child_environment(project_root, metadata),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30.0,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def preflight(project_root: Path = PROJECT_ROOT) -> bool:
    """只做无副作用检查；异常正文可能含配置细节，因此不向调用方回显。"""
    try:
        if not (project_root / "start_web.py").is_file():
            return False
        if not (project_root / "web" / "frontend" / "dist" / "index.html").is_file():
            return False
        metadata = read_json(RestartPaths(project_root).pid) or {}
        options = _prospective_startup_options(project_root)
        target_host = str(options["host"])
        target_port = int(options["port"])
        current_instance_id = metadata.get("instance_id")
        health_host_header = options.get("health_host_header")
        if health_host_header is not None:
            health_host_header = str(health_host_header)
        if not _port_is_free(target_host, target_port):
            old_health_host_header = metadata.get("health_host_header")
            candidate_headers = {
                str(value)
                for value in (old_health_host_header, health_host_header)
                if value is not None
            }
            candidate_headers.add("")
            belongs_to_current = isinstance(current_instance_id, str) and any(
                _health_instance(
                    target_host,
                    target_port,
                    host_header=header or None,
                )
                == current_instance_id
                for header in candidate_headers
            )
            if not belongs_to_current:
                return False
        return _isolated_startup_preflight(project_root, metadata)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False


def _health_url(host: str, port: int) -> str:
    health_host = "127.0.0.1" if host == "0.0.0.0" else host
    if health_host in {"::", "[::]"}:
        health_host = "::1"
    if ":" in health_host and not health_host.startswith("["):
        health_host = f"[{health_host}]"
    return f"http://{health_host}:{port}/healthz"


def _port_is_free(host: str, port: int) -> bool:
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    if connect_host in {"::", "[::]"}:
        connect_host = "::1"
    connect_host = connect_host.strip("[]")
    family = socket.AF_INET6 if ":" in connect_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            return client.connect_ex((connect_host, port)) != 0
    except OSError:
        return False


def _child_environment(project_root: Path, metadata: dict) -> dict[str, str]:
    environment = os.environ.copy()
    if not metadata:
        return environment
    overrides = set(metadata.get("environment_override_names", []))
    managed_names = set(metadata.get("dotenv_names", []))
    managed_names.update(dotenv_values(project_root / ".env"))
    for name in managed_names:
        if name not in overrides:
            environment.pop(name, None)
    return environment


def _spawn_flags() -> tuple[int, bool]:
    """Return creation flags/start-new-session for a detached replacement."""
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | subprocess.DETACHED_PROCESS
        )
        return flags, False
    return 0, True


def _same_instance(metadata: dict, *, pid: int, instance_id: str) -> bool:
    try:
        return int(metadata.get("pid", -1)) == pid and metadata.get("instance_id") == instance_id
    except (TypeError, ValueError):
        return False


def _stop_child(child: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    """Stop only the exact child created by this replacement controller."""

    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if child.poll() is None:
        try:
            child.kill()
            child.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _wait_for_healthy_child(
    paths: RestartPaths,
    child: subprocess.Popen[bytes],
    *,
    host: str,
    port: int,
    host_header: str | None,
    old_instance_id: str,
    timeout: float,
) -> str | None:
    deadline = time.monotonic() + timeout
    url = _health_url(host, port)
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        try:
            request = Request(url)
            if host_header:
                request.add_header("Host", host_header)
            with urlopen(request, timeout=1.0) as response:
                body = json.loads(response.read(64 * 1024).decode("utf-8"))
            candidate = body.get("instance_id")
            pid_metadata = read_json(paths.pid) or {}
            metadata_root = pid_metadata.get("project_root")
            if (
                response.status == 200
                and body.get("phase") == "running"
                and isinstance(candidate, str)
                and candidate != old_instance_id
                and _same_instance(pid_metadata, pid=child.pid, instance_id=candidate)
                and isinstance(metadata_root, str)
                and metadata_root
                and Path(metadata_root).resolve() == paths.root.resolve()
            ):
                return candidate
        except (OSError, URLError, UnicodeError, json.JSONDecodeError):
            pass
        time.sleep(0.25)
    return None


def _spawn_gateway(environment: dict[str, str]) -> subprocess.Popen[bytes]:
    creationflags, start_new_session = _spawn_flags()
    return subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "start_web.py")],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )


def _restore_old_environment(
    paths: RestartPaths,
    *,
    environment: dict[str, str],
    host: str,
    port: int,
    host_header: str | None,
    old_instance_id: str,
    timeout: float,
) -> str | None:
    rollback_environment = dict(environment)
    # The replacement inherited the old process environment after its .env
    # had been loaded. Do not mix newly edited .env-only names into rollback.
    rollback_environment["_KEMO_RESTART_SKIP_DOTENV"] = "1"
    try:
        child = _spawn_gateway(rollback_environment)
    except (OSError, ValueError):
        return None
    instance_id = _wait_for_healthy_child(
        paths,
        child,
        host=host,
        port=port,
        host_header=host_header,
        old_instance_id=old_instance_id,
        timeout=timeout,
    )
    if instance_id is None:
        _stop_child(child)
    return instance_id


def _replacement_process(args: argparse.Namespace) -> int:
    paths = RestartPaths(PROJECT_ROOT)
    old_environment = os.environ.copy()
    metadata = read_json(paths.pid) or {}
    lock = read_json(paths.lock) or {}
    metadata_root = metadata.get("project_root")
    if (
        lock.get("request_id") != args.request_id
        or not _same_instance(metadata, pid=args.old_pid, instance_id=args.old_instance_id)
        or not isinstance(metadata_root, str)
        or not metadata_root
        or Path(metadata_root).resolve() != PROJECT_ROOT.resolve()
    ):
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message="旧实例身份验证失败",
        )
        release_restart(paths)
        return 6

    old_host = str(metadata.get("host", "127.0.0.1"))
    old_port = int(metadata.get("port", 7531))
    try:
        target_options = _prospective_startup_options(PROJECT_ROOT)
        target_host = str(target_options["host"])
        target_port = int(target_options["port"])
        target_health_host_header = target_options.get("health_host_header")
        if target_health_host_header is not None:
            target_health_host_header = str(target_health_host_header)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message="新启动环境验证失败",
        )
        release_restart(paths)
        return 4

    stop_deadline = time.monotonic() + min(60.0, args.startup_timeout)
    while process_exists(args.old_pid) and time.monotonic() < stop_deadline:
        time.sleep(0.2)
    while not _port_is_free(old_host, old_port) and time.monotonic() < stop_deadline:
        time.sleep(0.2)
    old_alive = process_exists(args.old_pid)
    port_busy = not _port_is_free(old_host, old_port)
    if old_alive:
        # The old process was asked to stop gracefully.  Before escalating,
        # verify the PID file still names the same instance; never terminate a
        # PID that has already been reused by another process.
        current = read_json(paths.pid) or {}
        same_instance = _same_instance(
            current, pid=args.old_pid, instance_id=args.old_instance_id
        )
        if same_instance:
            terminate_process(args.old_pid)
            hard_deadline = time.monotonic() + 5.0
            while process_exists(args.old_pid) and time.monotonic() < hard_deadline:
                time.sleep(0.1)
            old_alive = process_exists(args.old_pid)
            if old_alive:
                # POSIX services may catch SIGTERM; use SIGKILL only after the
                # graceful window and the same-instance check above.
                current = read_json(paths.pid) or {}
                if _same_instance(
                    current, pid=args.old_pid, instance_id=args.old_instance_id
                ):
                    terminate_process(args.old_pid, force=True)
                    kill_deadline = time.monotonic() + 2.0
                    while process_exists(args.old_pid) and time.monotonic() < kill_deadline:
                        time.sleep(0.1)
                    old_alive = process_exists(args.old_pid)
    port_busy = not _port_is_free(old_host, old_port)
    if old_alive or port_busy:
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message=("旧实例未能在超时前退出" if old_alive else "旧实例端口未在超时前释放"),
        )
        release_restart(paths)
        return 4

    if not _port_is_free(target_host, target_port):
        old_health_host_header = metadata.get("health_host_header")
        restored_instance_id = _restore_old_environment(
            paths,
            environment=old_environment,
            host=old_host,
            port=old_port,
            host_header=(
                str(old_health_host_header)
                if old_health_host_header is not None
                else None
            ),
            old_instance_id=args.old_instance_id,
            timeout=args.startup_timeout,
        )
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message=(
                "新实例目标端口已被占用，已自动恢复旧启动环境"
                if restored_instance_id
                else "新实例目标端口已被占用，旧启动环境恢复失败"
            ),
            new_instance_id=restored_instance_id,
        )
        release_restart(paths)
        return 4

    # 防止新实例的 watcher 重复处理同一请求；状态和锁保留到健康检查结束。
    paths.request.unlink(missing_ok=True)
    write_restart_status(
        paths,
        request_id=args.request_id,
        phase="starting",
        message="正在启动新实例",
    )
    try:
        child = _spawn_gateway(_child_environment(PROJECT_ROOT, metadata))
    except (OSError, ValueError):
        old_health_host_header = metadata.get("health_host_header")
        restored_instance_id = _restore_old_environment(
            paths,
            environment=old_environment,
            host=old_host,
            port=old_port,
            host_header=(
                str(old_health_host_header)
                if old_health_host_header is not None
                else None
            ),
            old_instance_id=args.old_instance_id,
            timeout=args.startup_timeout,
        )
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message=(
                "无法创建新网关进程，已自动恢复旧启动环境"
                if restored_instance_id
                else "无法创建新网关进程，旧启动环境恢复失败"
            ),
            new_instance_id=restored_instance_id,
        )
        release_restart(paths)
        return 4

    new_instance_id = _wait_for_healthy_child(
        paths,
        child,
        host=target_host,
        port=target_port,
        host_header=target_health_host_header,
        old_instance_id=args.old_instance_id,
        timeout=args.startup_timeout,
    )

    if new_instance_id is None:
        _stop_child(child)
        old_health_host_header = metadata.get("health_host_header")
        restored_instance_id = _restore_old_environment(
            paths,
            environment=old_environment,
            host=old_host,
            port=old_port,
            host_header=(
                str(old_health_host_header)
                if old_health_host_header is not None
                else None
            ),
            old_instance_id=args.old_instance_id,
            timeout=args.startup_timeout,
        )
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message=(
                "新配置未通过健康检查，已自动恢复旧启动环境"
                if restored_instance_id
                else "新实例未通过健康检查，旧启动环境恢复也失败"
            ),
            new_instance_id=restored_instance_id,
        )
        release_restart(paths)
        return 4

    write_restart_status(
        paths,
        request_id=args.request_id,
        phase="succeeded",
        message="网关重启成功",
        new_instance_id=new_instance_id,
    )
    release_restart(paths)
    return 0


def _print_status(paths: RestartPaths) -> int:
    status = read_json(paths.status) or {
        "request_id": None,
        "phase": "idle",
        "message": "当前没有重启任务",
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def _submit_and_wait(args: argparse.Namespace) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    paths = RestartPaths(PROJECT_ROOT)
    if not preflight(PROJECT_ROOT):
        print("[ERROR] 重启前检查失败，旧网关未受影响。", file=sys.stderr)
        return 2
    metadata = read_json(paths.pid) or {}
    pid = int(metadata.get("pid", -1))
    metadata_root = metadata.get("project_root")
    if (
        not process_exists(pid)
        or not isinstance(metadata_root, str)
        or not metadata_root
        or Path(metadata_root).resolve() != PROJECT_ROOT.resolve()
    ):
        print("[ERROR] 没有找到由 start_web.py 启动的当前网关实例。", file=sys.stderr)
        return 6
    try:
        health_host_header = metadata.get("health_host_header")
        with _open_health(
            str(metadata.get("host", "127.0.0.1")),
            int(metadata.get("port", 7531)),
            host_header=(
                str(health_host_header) if health_host_header is not None else None
            ),
            timeout=2.0,
        ) as response:
            health = json.loads(response.read(64 * 1024).decode("utf-8"))
        if health.get("instance_id") != metadata.get("instance_id"):
            raise ValueError("instance mismatch")
    except (OSError, URLError, UnicodeError, ValueError, json.JSONDecodeError):
        print("[ERROR] 当前 PID 与健康检查实例不一致，已拒绝重启。", file=sys.stderr)
        return 6
    request = RestartRequest.create(
        reason=args.reason,
        force=args.force,
        requested_by="local-cli",
        drain_timeout_seconds=_timeout("RESTART_DRAIN_TIMEOUT", 120.0),
        startup_timeout_seconds=_timeout("RESTART_STARTUP_TIMEOUT", 60.0),
    )
    try:
        submit_restart(paths, request, pid)
    except RestartAlreadyRunning:
        print("[ERROR] 已有重启任务正在执行。", file=sys.stderr)
        return 5
    print(f"[KEMO] 已提交重启请求: {request.request_id}")
    if args.no_wait:
        return 0

    deadline = time.monotonic() + request.drain_timeout_seconds + request.startup_timeout_seconds + 30
    while time.monotonic() < deadline:
        status = read_json(paths.status) or {}
        if status.get("request_id") == request.request_id and status.get("phase") in TERMINAL_PHASES:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status.get("phase") == "succeeded" else 4
        time.sleep(0.4)
    print("[ERROR] 等待重启结果超时，可使用 --status 查询。", file=sys.stderr)
    return 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kemo 网关平滑重启")
    parser.add_argument("--reason", default="manual restart", help="重启原因")
    parser.add_argument("--force", action="store_true", help="Drain 超时后仍继续重启")
    parser.add_argument("--no-wait", action="store_true", help="提交后立即返回")
    parser.add_argument("--status", action="store_true", help="显示最近一次重启状态")
    parser.add_argument("--preflight", action="store_true", help="只执行重启前检查")
    parser.add_argument("--replace", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--request-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--old-pid", type=int, default=-1, help=argparse.SUPPRESS)
    parser.add_argument("--old-instance-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--startup-timeout", type=float, default=60.0, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = RestartPaths(PROJECT_ROOT)
    if args.status:
        return _print_status(paths)
    if args.preflight:
        passed = preflight(PROJECT_ROOT)
        print("[OK] 重启前检查通过" if passed else "[ERROR] 重启前检查失败")
        return 0 if passed else 2
    if args.replace:
        return _replacement_process(args)
    return _submit_and_wait(args)


if __name__ == "__main__":
    raise SystemExit(main())
