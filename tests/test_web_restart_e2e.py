from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest

from core.restart_control import process_exists, read_json, terminate_process


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_RESTART_E2E = os.getenv("KEMO_RUN_RESTART_E2E", "").strip() == "1"
HTTP = build_opener(ProxyHandler({}))


def _copy_runtime_project(destination: Path) -> None:
    def ignore_runtime(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name == "__pycache__"
            or name.endswith((".pyc", ".pyo"))
            or name.endswith(".json")
            or name == "runtime"
        }

    for filename in (
        "start_web.py",
        "restart.py",
        "version.json",
        "kemo-adapter-api.png",
    ):
        shutil.copy2(PROJECT_ROOT / filename, destination / filename)
    shutil.copytree(PROJECT_ROOT / "api", destination / "api", ignore=ignore_runtime)
    shutil.copytree(PROJECT_ROOT / "core", destination / "core", ignore=ignore_runtime)
    (destination / "web").mkdir()
    shutil.copy2(PROJECT_ROOT / "web" / "__init__.py", destination / "web" / "__init__.py")
    shutil.copytree(PROJECT_ROOT / "web" / "backend", destination / "web" / "backend", ignore=ignore_runtime)
    frontend_dist = PROJECT_ROOT / "web" / "frontend" / "dist"
    if not (frontend_dist / "index.html").is_file():
        raise AssertionError("Web 前端尚未构建，无法执行网页重启端到端测试")
    shutil.copytree(frontend_dist, destination / "web" / "frontend" / "dist")
    (destination / "providers").mkdir()
    shutil.copy2(
        PROJECT_ROOT / "providers" / "__init__.py",
        destination / "providers" / "__init__.py",
    )
    (destination / "storage").mkdir()
    shutil.copy2(
        PROJECT_ROOT / "storage" / "__init__.py",
        destination / "storage" / "__init__.py",
    )
    shutil.copy2(
        PROJECT_ROOT / "storage" / "statistics.py",
        destination / "storage" / "statistics.py",
    )
    (destination / "api" / "runtime.json").write_text(
        '{"gateway_api":{"enabled":true}}\n', encoding="utf-8"
    )
    (destination / "api" / "keys.json").write_text(
        '{"keys":{}}\n', encoding="utf-8"
    )
    (destination / "core" / "live_control.json").write_text(
        (
            '{"highest_priority_system_prompt":"",'
            '"disabled_providers":[],"disabled_models":[]}\n'
        ),
        encoding="utf-8",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _write_env(project: Path, port: str | int) -> None:
    (project / ".env").write_text(
        "\n".join(
            (
                "HOST=127.0.0.1",
                f"PORT={port}",
                "WEB_OPEN_BROWSER=false",
                "WEB_ACCESS_LOG=false",
                "WEB_TOKEN=ci-web-token",
                "WEB_USERNAME=ci-owner",
                "WEB_PASSWORD=ci-password",
                "STATUS_TOKEN=",
                "API_DOCS_ENABLED=false",
                "RESTART_DRAIN_TIMEOUT=10",
                "RESTART_STARTUP_TIMEOUT=20",
                "STATISTICS_TIMEZONE=UTC",
                "",
            )
        ),
        encoding="utf-8",
    )


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(f"{base_url}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("Origin", base_url)
    with HTTP.open(request, timeout=2.0) as response:
        return json.loads(response.read(64 * 1024).decode("utf-8"))


def _client_json(
    client,
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    csrf_token: str | None = None,
) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(f"{base_url}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("Origin", base_url)
    if csrf_token:
        request.add_header("X-CSRF-Token", csrf_token)
    with client.open(request, timeout=2.0) as response:
        return json.loads(response.read(64 * 1024).decode("utf-8"))


def _wait_for_health(
    base_url: str,
    *,
    timeout: float,
    different_from: str | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            health = _request_json(base_url, "/healthz")
            instance_id = health.get("instance_id")
            if (
                health.get("phase") == "running"
                and isinstance(instance_id, str)
                and instance_id != different_from
            ):
                return health
        except (OSError, URLError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"网关未在 {timeout:.0f} 秒内通过健康检查: {last_error}")


def _wait_for_restart_phase(
    client,
    base_url: str,
    request_id: str,
    phase: str,
    *,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_status: dict[str, object] | None = None
    while time.monotonic() < deadline:
        try:
            status = _client_json(client, base_url, "/admin/api/system/restart")
            restart = status.get("restart")
            if isinstance(restart, dict):
                last_status = restart
                if restart.get("request_id") == request_id and restart.get("phase") == phase:
                    return restart
        except (OSError, URLError, UnicodeError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"重启状态未进入 {phase}: {last_status}")


def _stop_gateway(project: Path) -> None:
    metadata = read_json(project / "core" / "runtime" / "gateway.pid.json") or {}
    metadata_root = metadata.get("project_root")
    try:
        pid = int(metadata.get("pid", -1))
    except (TypeError, ValueError):
        return
    if (
        pid <= 0
        or pid == os.getpid()
        or not isinstance(metadata_root, str)
        or Path(metadata_root).resolve() != project.resolve()
    ):
        return
    terminate_process(pid)
    deadline = time.monotonic() + 5.0
    while process_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_exists(pid):
        terminate_process(pid, force=True)


@pytest.mark.skipif(
    not RUN_RESTART_E2E,
    reason="设置 KEMO_RUN_RESTART_E2E=1 才执行真实进程替换测试",
)
def test_web_restart_rejects_invalid_env_then_restarts_on_new_port(
    tmp_path: Path,
) -> None:
    project = tmp_path / "gateway"
    project.mkdir()
    _copy_runtime_project(project)
    first_port = _free_port()
    second_port = _free_port()
    while second_port == first_port:
        second_port = _free_port()
    _write_env(project, first_port)

    environment = os.environ.copy()
    for name in list(environment):
        if name in {"HOST", "PORT", "STATUS_TOKEN"} or name.startswith(
            ("WEB_", "GATEWAY_", "PROVIDER_", "RESTART_")
        ):
            environment.pop(name, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
    environment["no_proxy"] = environment["NO_PROXY"]
    environment["PYTHONUNBUFFERED"] = "1"

    log_path = project / "initial-gateway.log"
    initial: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("ab", buffering=0) as output:
            initial = subprocess.Popen(
                [sys.executable, str(project / "start_web.py")],
                cwd=project,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )

        first_url = f"http://127.0.0.1:{first_port}"
        first_health = _wait_for_health(first_url, timeout=20.0)
        first_instance_id = str(first_health["instance_id"])
        client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        token_result = _client_json(
            client,
            first_url,
            "/admin/api/auth/token",
            method="POST",
            payload={"token": "ci-web-token"},
        )
        assert token_result["next_step"] == "password"
        password_result = _client_json(
            client,
            first_url,
            "/admin/api/auth/password",
            method="POST",
            payload={"username": "ci-owner", "password": "ci-password"},
        )
        csrf_token = str(password_result["csrf_token"])
        with client.open(f"{first_url}/admin", timeout=2.0) as response:
            assert response.status == 200

        # A broken startup environment must fail preflight while the old
        # gateway remains healthy and keeps the same instance identity.
        _write_env(project, "invalid-port")
        rejected = _client_json(
            client,
            first_url,
            "/admin/api/system/restart",
            method="POST",
            payload={"reason": "ci invalid environment", "force": False},
            csrf_token=csrf_token,
        )
        rejected_id = str(rejected["request_id"])
        _wait_for_restart_phase(
            client, first_url, rejected_id, "failed", timeout=20.0
        )
        assert _wait_for_health(first_url, timeout=3.0)["instance_id"] == first_instance_id
        assert initial.poll() is None

        # The success path changes PORT before using the real Web owner API.
        # The replacement must stop the old PID, read the new .env, bind the
        # new port, publish new PID metadata, and pass /healthz.
        _write_env(project, second_port)
        accepted = _client_json(
            client,
            first_url,
            "/admin/api/system/restart",
            method="POST",
            payload={"reason": "ci web restart", "force": False},
            csrf_token=csrf_token,
        )
        accepted_id = str(accepted["request_id"])
        initial.wait(timeout=30.0)

        second_url = f"http://127.0.0.1:{second_port}"
        second_health = _wait_for_health(
            second_url, timeout=30.0, different_from=first_instance_id
        )
        second_instance_id = str(second_health["instance_id"])
        succeeded = _wait_for_restart_phase(
            client, second_url, accepted_id, "succeeded", timeout=10.0
        )
        assert succeeded["new_instance_id"] == second_instance_id

        # The same browser cookie and CSRF token must remain valid after the
        # new process loads its persisted session handoff.
        session = _client_json(client, second_url, "/admin/api/auth/session")
        assert session["authenticated"] is True
        assert session["csrf_token"] == csrf_token

        metadata = read_json(project / "core" / "runtime" / "gateway.pid.json")
        assert metadata is not None
        assert int(metadata["pid"]) != initial.pid
        assert metadata["instance_id"] == second_instance_id
        assert int(metadata["port"]) == second_port
        assert Path(str(metadata["project_root"])).resolve() == project.resolve()
        with client.open(f"{second_url}/admin", timeout=2.0) as response:
            assert response.status == 200
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            assert client.connect_ex(("127.0.0.1", first_port)) != 0
    except Exception as exc:
        log_tail = ""
        if log_path.exists():
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
        restart_log = project / "core" / "runtime" / "restart-process.log"
        if restart_log.exists():
            log_tail += "\n--- restart-process.log ---\n"
            log_tail += restart_log.read_text(encoding="utf-8", errors="replace")[-8000:]
        raise AssertionError(f"网页重启端到端测试失败：{exc}\n{log_tail}") from exc
    finally:
        _stop_gateway(project)
        if initial is not None and initial.poll() is None:
            initial.terminate()
            try:
                initial.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                initial.kill()
