from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

import pytest

from core.restart_control import process_exists, read_json, terminate_process
from core.yaml_config import startup_values

from tests.support.paths import PROJECT_ROOT
RUN_RESTART_E2E = os.getenv("KEMO_RUN_RESTART_E2E", "").strip() == "1"
HTTP = build_opener(ProxyHandler({}))


def _install_runtime_project(destination: Path, environment: dict[str, str]) -> Path:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=PROJECT_ROOT, check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    for relative in tracked.split("\0"):
        if relative:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT_ROOT / relative, target)

    assert not (destination / "gateway.yaml").exists()
    assert not (destination / "data").exists()
    assert not (destination / ".venv").exists()
    assert not (destination / "web" / "frontend" / "dist").exists()
    subprocess.run(
        [sys.executable, "setup.py"], cwd=destination, env=environment,
        check=True, timeout=900,
    )
    assert (destination / "gateway.yaml").read_bytes() == (
        destination / "gateway.yaml.example"
    ).read_bytes()
    assert (destination / "web" / "frontend" / "dist" / "index.html").is_file()
    executable = destination / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    assert executable.is_file()
    return executable


def _check_console_assets(project: Path, base_url: str) -> None:
    frontend_dist = project / "web" / "frontend" / "dist"
    with HTTP.open(f"{base_url}/admin", timeout=2.0) as response:
        assert response.status == 200
        index = response.read()
        assert index == (frontend_dist / "index.html").read_bytes()
    assets = {
        urlsplit(path).path
        for path in re.findall(r'(?:src|href)="([^"]+)"', index.decode("utf-8"))
        if path.startswith("/admin/assets/")
    }
    assert any(path.endswith(".js") for path in assets)
    assert any(path.endswith(".css") for path in assets)
    for path in assets:
        with HTTP.open(f"{base_url}{path}", timeout=2.0) as response:
            assert response.status == 200
            assert response.read() == (frontend_dist / path.removeprefix("/admin/")).read_bytes()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _write_env(project: Path, port: str | int) -> None:
    (project / "gateway.yaml").write_text(
        "\n".join(
            (
                # Bind all interfaces so this E2E exercises the protected
                # Token -> password path instead of the trusted loopback
                # owner bypass. The client still connects through 127.0.0.1.
                "HOST: 0.0.0.0",
                f"PORT: {port}",
                "WEB_OPEN_BROWSER: false",
                "WEB_ACCESS_LOG: false",
                "WEB_TOKEN: ci-web-token",
                "WEB_USERNAME: ci-owner",
                "WEB_PASSWORD: ci-password",
                "STATUS_TOKEN: ",
                "API_DOCS_ENABLED: false",
                "RESTART_DRAIN_TIMEOUT: 10",
                "RESTART_STARTUP_TIMEOUT: 20",
                "STATISTICS_TIMEZONE: UTC",
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
    reason="设置 KEMO_RUN_RESTART_E2E: 1 才执行真实进程替换测试",
)
def test_web_restart_rejects_invalid_env_then_restarts_on_new_port(
    tmp_path: Path,
) -> None:
    project = tmp_path / "gateway"
    project.mkdir()
    first_port = _free_port()
    second_port = _free_port()
    while second_port == first_port:
        second_port = _free_port()

    environment = os.environ.copy()
    startup_names = set(startup_values(PROJECT_ROOT / "gateway.yaml.example"))
    for name in list(environment):
        if name in startup_names | {"KEMO_MASTER_KEY"} or name.startswith(
            ("WEB_", "GATEWAY_", "PROVIDER_", "RESTART_")
        ):
            environment.pop(name, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
    environment["no_proxy"] = environment["NO_PROXY"]
    environment["PYTHONUNBUFFERED"] = "1"
    executable = _install_runtime_project(project, environment)

    log_path = project / "initial-gateway.log"
    initial: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("ab", buffering=0) as output:
            initial = subprocess.Popen(
                [str(executable), str(project / "start_web.py")],
                cwd=project,
                env={**environment, "PORT": str(first_port)},
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        first_url = f"http://127.0.0.1:{first_port}"
        _wait_for_health(first_url, timeout=20.0)
        _check_console_assets(project, first_url)
        console = _request_json(first_url, "/admin/api/console")
        assert console["version"]["version"] == json.loads(
            (project / "version.json").read_text(encoding="utf-8")
        )["version"]
        for collection in ("upstreams", "models", "deployments", "keys"):
            assert console[collection] == []
        _stop_gateway(project)
        initial.wait(timeout=10.0)

        _write_env(project, first_port)
        with log_path.open("ab", buffering=0) as output:
            initial = subprocess.Popen(
                [str(executable), str(project / "start_web.py")],
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
        terminal = _client_json(client, first_url, "/admin/api/system/terminal-logs")
        assert terminal["capturing"] is True
        assert any("[KEMO] Web" in row["text"] for row in terminal["items"])
        assert "ci-password" not in json.dumps(terminal)

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
        # The replacement must stop the old PID, read the new gateway.yaml, bind the
        # new port, publish new PID metadata, and pass /healthz.
        pending_config = _client_json(client, first_url, "/admin/api/system/startup-config")
        edited_config = str(pending_config["yaml"]).replace("PORT: invalid-port", f"PORT: {second_port}")
        assert f"PORT: {second_port}" in edited_config
        saved_config = _client_json(
            client, first_url, "/admin/api/system/startup-config", method="PUT",
            payload={"expected_revision": pending_config["expected_revision"], "yaml": edited_config},
            csrf_token=csrf_token,
        )
        assert saved_config["restart_required"] is True
        assert "ci-password" not in json.dumps(saved_config)
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
        terminal = _client_json(client, second_url, "/admin/api/system/terminal-logs")
        assert terminal["capturing"] is True
        assert any(f":{second_port}" in row["text"] for row in terminal["items"])

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
