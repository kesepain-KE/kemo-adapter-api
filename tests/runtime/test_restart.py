from __future__ import annotations

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import restart
from api.server import create_app
from core.config import Settings
from core.restart_control import (
    RestartAlreadyRunning,
    RestartPaths,
    RestartRequest,
    atomic_json,
    clear_pid_metadata,
    read_json,
    release_restart,
    submit_restart,
    write_pid_metadata,
)
from restart import _child_environment, _prospective_environment
from core.runtime_state import GatewayDrainingError, GatewayPhase, GatewayRuntimeState
from tests.support.admin import ADMIN_HEADERS, admin_project, admin_settings
from web.backend.auth_service import WebAuthService


OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


@pytest.mark.parametrize(
    ("pid", "parent_pid", "same_root", "instance_id", "accepted"),
    [
        (43210, 100, True, "instance_new", True),
        (43211, 43210, True, "instance_new", os.name == "nt"),
        (43211, 100, True, "instance_new", False),
        (43210, 100, False, "instance_new", False),
        (43210, 100, True, "instance_other", False),
    ],
)
def test_replacement_health_requires_own_process_and_instance(
    tmp_path, monkeypatch, pid, parent_pid, same_root, instance_id, accepted
):
    paths = RestartPaths(tmp_path)
    atomic_json(paths.pid, {
        "pid": pid, "parent_pid": parent_pid, "instance_id": instance_id,
        "project_root": str(tmp_path if same_root else tmp_path / "other"),
    })

    def health(*_args, **_kwargs):
        response = BytesIO(json.dumps({
            "phase": "running", "instance_id": "instance_new",
        }).encode("utf-8"))
        response.status = 200
        return response

    monkeypatch.setattr(restart, "urlopen", health)
    child = SimpleNamespace(pid=43210, poll=lambda: None)
    result = restart._wait_for_healthy_child(
        paths, child, host="127.0.0.1", port=7531, host_header=None,
        old_instance_id="instance_old", timeout=.01,
    )
    assert result == ("instance_new" if accepted else None)


@pytest.mark.skipif(os.name != "nt", reason="Windows venv interpreter launcher")
@pytest.mark.parametrize("same_root", [True, False])
def test_failed_replacement_stops_only_owned_venv_interpreter(
    tmp_path, monkeypatch, same_root
):
    paths = RestartPaths(tmp_path)
    atomic_json(paths.pid, {
        "pid": 43211, "parent_pid": 43210,
        "project_root": str(tmp_path if same_root else tmp_path / "other"),
    })
    stopped = []
    monkeypatch.setattr(restart, "terminate_process", lambda pid: stopped.append(pid))
    child = SimpleNamespace(
        pid=43210, poll=lambda: None,
        terminate=lambda: stopped.append(43210), wait=lambda **_kwargs: None,
    )

    restart._stop_child(paths, child)

    assert stopped == ([43211, 43210] if same_root else [43210])


@pytest.mark.skipif(os.name != "nt", reason="Windows denies replacement while a regular file reader is open")
@pytest.mark.parametrize("release_reader", [True, False])
def test_atomic_status_write_handles_windows_file_reader(tmp_path, release_reader):
    path = tmp_path / "restart-status.json"
    atomic_json(path, {"phase": "starting"})
    reader = path.open("rb")
    timer = threading.Timer(.04, reader.close) if release_reader else None
    try:
        if timer:
            timer.start()
            atomic_json(path, {"phase": "succeeded"})
            assert read_json(path) == {"phase": "succeeded"}
        else:
            with pytest.raises(PermissionError):
                atomic_json(path, {"phase": "succeeded"})
            assert read_json(path) == {"phase": "starting"}
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        reader.close()
        if timer:
            timer.join()


def test_runtime_state_drains_existing_execution_and_rejects_new_work() -> None:
    async def scenario() -> None:
        state = GatewayRuntimeState()
        await state.mark_running()
        lease = await state.admit_execution()
        assert state.active_executions == 1

        await state.begin_drain(reason="test", requested_by="owner")
        assert state.phase == GatewayPhase.DRAINING
        with pytest.raises(GatewayDrainingError):
            await state.admit_execution()
        assert await state.wait_for_idle(0.02) is False

        await lease.release()
        assert await state.wait_for_idle(0.1) is True
        await state.cancel_drain()
        assert state.phase == GatewayPhase.RUNNING

    asyncio.run(scenario())


def test_restart_submission_uses_exclusive_lock_and_atomic_status(tmp_path: Path) -> None:
    paths = RestartPaths(tmp_path)
    request = RestartRequest.create(
        reason="test restart",
        force=False,
        requested_by="test-owner",
        drain_timeout_seconds=10,
        startup_timeout_seconds=10,
    )
    submit_restart(paths, request, gateway_pid=999999)
    assert read_json(paths.request)["request_id"] == request.request_id
    assert read_json(paths.status)["phase"] == "queued"

    second = RestartRequest.create(
        reason="second",
        force=False,
        requested_by="test-owner",
        drain_timeout_seconds=10,
        startup_timeout_seconds=10,
    )
    # 第一把锁的 PID 不存在，允许安全回收；改成当前 PID 后必须拒绝并发提交。
    paths.lock.write_text(
        '{"request_id":"active","gateway_pid":' + str(__import__("os").getpid()) + "}",
        encoding="utf-8",
    )
    with pytest.raises(RestartAlreadyRunning):
        submit_restart(paths, second, gateway_pid=__import__("os").getpid())
    release_restart(paths)


def test_restart_admin_api_requires_owner_and_returns_queue_id(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    app = create_app(admin_settings(), live_config_root=root, )

    class FakeRestartService:
        def status(self):
            return {"request_id": None, "phase": "idle", "message": "idle"}

        def enqueue(self, *, reason: str, force: bool, requested_by: str):
            assert reason == "deploy update"
            assert force is False
            assert requested_by == "owner-console"
            return SimpleNamespace(request_id="restart_test", force=False)

    with TestClient(app) as client:
        app.state.restart_service = FakeRestartService()
        assert client.get("/admin/api/system/restart", headers=ADMIN_HEADERS).status_code == 403
        status = client.get("/admin/api/system/restart", headers=OWNER_HEADERS)
        assert status.status_code == 200
        assert status.json()["gateway"]["phase"] == "running"

        queued = client.post(
            "/admin/api/system/restart",
            headers=OWNER_HEADERS,
            json={"reason": "deploy update", "force": False},
        )
        assert queued.status_code == 202
        assert queued.json() == {
            "request_id": "restart_test",
            "status": "queued",
            "force": False,
        }


def test_replacement_environment_reloads_changed_startup_config_and_drops_removed_values(
    tmp_path: Path, monkeypatch
) -> None:
    """A replacement must not inherit values loaded by the old process."""
    env_file = tmp_path / "gateway.yaml"
    env_file.write_text("PORT: 8754\nNEW_SETTING: updated\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "old-loaded-value")
    monkeypatch.setenv("REMOVED_SETTING", "stale-value")
    metadata = {
        # PORT and REMOVED_SETTING were loaded from the previous gateway.yaml, not
        # supplied by the shell, so both must be removed before start_web.py
        # calls load_startup_environment(override=False).
        "environment_override_names": [],
        "startup_names": ["PORT", "REMOVED_SETTING"],
    }

    child = _child_environment(tmp_path, metadata)

    assert "PORT" not in child
    assert "REMOVED_SETTING" not in child
    # start_web.py will load the new value from the current gateway.yaml.
    assert env_file.read_text(encoding="utf-8") == "PORT: 8754\nNEW_SETTING: updated\n"


def test_replacement_environment_preserves_explicit_process_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "gateway.yaml").write_text("PORT: 8754\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "9999")
    child = _child_environment(
        tmp_path,
        {"environment_override_names": ["PORT"], "startup_names": ["PORT"]},
    )
    assert child["PORT"] == "9999"


def test_prospective_environment_restores_running_startup_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RestartPaths(tmp_path)
    monkeypatch.setenv("WEB_TOKEN", "old-running-token")
    (tmp_path / "gateway.yaml").write_text("WEB_TOKEN: new-token\n", encoding="utf-8")
    write_pid_metadata(
        paths,
        pid=43210,
        instance_id="instance_old",
        host="127.0.0.1",
        port=7531,
        environment_override_names=[],
        startup_names=["WEB_TOKEN"],
    )

    with _prospective_environment(tmp_path):
        assert os.environ["WEB_TOKEN"] == "new-token"

    assert os.environ["WEB_TOKEN"] == "old-running-token"


def test_pid_metadata_is_retained_only_during_restart_handoff(tmp_path: Path) -> None:
    paths = RestartPaths(tmp_path)
    write_pid_metadata(
        paths,
        pid=43210,
        instance_id="instance_old",
        host="127.0.0.1",
        port=7531,
        environment_override_names=[],
    )
    paths.lock.write_text(
        '{"request_id":"restart_test","gateway_pid":43210}',
        encoding="utf-8",
    )

    clear_pid_metadata(paths, "instance_old")
    assert read_json(paths.pid)["instance_id"] == "instance_old"

    paths.lock.unlink()
    clear_pid_metadata(paths, "instance_old")
    assert not paths.pid.exists()


def test_web_session_survives_restart_without_persisting_plain_cookie(
    tmp_path: Path,
) -> None:
    path = tmp_path / "web-sessions.json"
    first = WebAuthService(
        ttl_seconds=7200,
        persistence_path=path,
        namespace="credentials-v1",
    )
    issued = first.issue(stage="complete")

    raw = path.read_text(encoding="utf-8")
    assert issued.token not in raw
    assert issued.csrf_token in raw

    restored = WebAuthService(
        ttl_seconds=7200,
        persistence_path=path,
        namespace="credentials-v1",
    ).resolve(issued.token, stage="complete")
    assert restored is not None
    assert restored.csrf_token == issued.csrf_token
    assert restored.token == ""


def test_web_session_is_invalidated_when_authentication_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "web-sessions.json"
    first = WebAuthService(
        persistence_path=path,
        namespace="credentials-v1",
    )
    issued = first.issue(stage="complete")

    changed = WebAuthService(
        persistence_path=path,
        namespace="credentials-v2",
    )
    assert changed.resolve(issued.token, stage="complete") is None
