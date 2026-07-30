from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from core.restart_control import (
    RestartAlreadyRunning,
    RestartPaths,
    RestartRequest,
    read_json,
    release_restart,
    submit_restart,
)
from restart import _child_environment
from core.runtime_state import GatewayDrainingError, GatewayPhase, GatewayRuntimeState
from tests.test_admin_api import ADMIN_HEADERS, admin_project


OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


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
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

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


def test_replacement_environment_reloads_changed_dotenv_and_drops_removed_values(
    tmp_path: Path, monkeypatch
) -> None:
    """A replacement must not inherit values loaded by the old process."""
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=8754\nNEW_SETTING=updated\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "old-loaded-value")
    monkeypatch.setenv("REMOVED_SETTING", "stale-value")
    metadata = {
        # PORT and REMOVED_SETTING were loaded from the previous .env, not
        # supplied by the shell, so both must be removed before start_web.py
        # calls load_dotenv(override=False).
        "environment_override_names": [],
        "dotenv_names": ["PORT", "REMOVED_SETTING"],
    }

    child = _child_environment(tmp_path, metadata)

    assert "PORT" not in child
    assert "REMOVED_SETTING" not in child
    # start_web.py will load the new value from the current .env.
    assert env_file.read_text(encoding="utf-8") == "PORT=8754\nNEW_SETTING=updated\n"


def test_replacement_environment_preserves_explicit_process_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".env").write_text("PORT=8754\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "9999")
    child = _child_environment(
        tmp_path,
        {"environment_override_names": ["PORT"], "dotenv_names": ["PORT"]},
    )
    assert child["PORT"] == "9999"
