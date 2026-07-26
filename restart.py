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
from urllib.request import urlopen

from dotenv import dotenv_values, load_dotenv

from core.restart_control import (
    RestartAlreadyRunning,
    RestartPaths,
    RestartRequest,
    process_exists,
    read_json,
    release_restart,
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
    values = dotenv_values(project_root / ".env")
    previous: dict[str, str | None] = {}
    try:
        for name, value in values.items():
            if name in override_names:
                continue
            previous[name] = os.environ.get(name)
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


def preflight(project_root: Path = PROJECT_ROOT) -> bool:
    """只做无副作用检查；异常正文可能含配置细节，因此不向调用方回显。"""
    try:
        if not (project_root / "start_web.py").is_file():
            return False
        if not (project_root / "web" / "frontend" / "dist" / "index.html").is_file():
            return False
        with _prospective_environment(project_root):
            from start_web import _startup_options

            _startup_options()
        return True
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False


def _health_url(host: str, port: int) -> str:
    health_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    if ":" in health_host and not health_host.startswith("["):
        health_host = f"[{health_host}]"
    return f"http://{health_host}:{port}/healthz"


def _port_is_free(host: str, port: int) -> bool:
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    family = socket.AF_INET6 if ":" in connect_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            return client.connect_ex((connect_host, port)) != 0
    except OSError:
        return False


def _child_environment(project_root: Path, metadata: dict) -> dict[str, str]:
    environment = os.environ.copy()
    overrides = set(metadata.get("environment_override_names", []))
    for name in dotenv_values(project_root / ".env"):
        if name not in overrides:
            environment.pop(name, None)
    return environment


def _replacement_process(args: argparse.Namespace) -> int:
    paths = RestartPaths(PROJECT_ROOT)
    metadata = read_json(paths.pid) or {}
    lock = read_json(paths.lock) or {}
    metadata_root = metadata.get("project_root")
    if (
        lock.get("request_id") != args.request_id
        or int(metadata.get("pid", -1)) != args.old_pid
        or metadata.get("instance_id") != args.old_instance_id
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

    host = str(metadata.get("host", "127.0.0.1"))
    port = int(metadata.get("port", 7531))
    stop_deadline = time.monotonic() + min(60.0, args.startup_timeout)
    while process_exists(args.old_pid) and time.monotonic() < stop_deadline:
        time.sleep(0.2)
    while not _port_is_free(host, port) and time.monotonic() < stop_deadline:
        time.sleep(0.2)
    if process_exists(args.old_pid) or not _port_is_free(host, port):
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message="旧实例未能在超时前退出",
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
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        child = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "start_web.py")],
            cwd=PROJECT_ROOT,
            env=_child_environment(PROJECT_ROOT, metadata),
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError:
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message="无法创建新网关进程",
        )
        release_restart(paths)
        return 4

    deadline = time.monotonic() + args.startup_timeout
    url = _health_url(host, port)
    new_instance_id: str | None = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        try:
            with urlopen(url, timeout=1.0) as response:
                body = json.loads(response.read(64 * 1024).decode("utf-8"))
            candidate = body.get("instance_id")
            if (
                response.status == 200
                and body.get("phase") == "running"
                and isinstance(candidate, str)
                and candidate != args.old_instance_id
            ):
                new_instance_id = candidate
                break
        except (OSError, URLError, UnicodeError, json.JSONDecodeError):
            pass
        time.sleep(0.25)

    if new_instance_id is None:
        if child.poll() is None:
            child.terminate()
        write_restart_status(
            paths,
            request_id=args.request_id,
            phase="failed",
            message="新实例未通过健康检查",
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
    release_restart(paths, remove_request=False)
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
        with urlopen(
            _health_url(str(metadata.get("host", "127.0.0.1")), int(metadata.get("port", 7531))),
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
