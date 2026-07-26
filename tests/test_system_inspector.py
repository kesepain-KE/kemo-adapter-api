from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

from web.backend.system_inspector import REMOTE_VERSION_URL, SystemInspector


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_restart_detection_ignores_hot_config_and_tracks_startup_files(
    tmp_path: Path,
) -> None:
    write(tmp_path / ".env", "PORT=7531\n")
    write(tmp_path / "api" / "server.py", "APP_VERSION = 1\n")
    write(tmp_path / "api" / "runtime.json", "{}\n")
    write(tmp_path / "core" / "live_control.json", "{}\n")
    write(tmp_path / "providers" / "fake" / "config.json", "{}\n")
    write(tmp_path / "providers" / "fake" / "secrets.json", "{}\n")
    write(tmp_path / "providers" / "fake" / "manifest.json", "{}\n")
    inspector = SystemInspector(tmp_path)

    write(tmp_path / "api" / "runtime.json", '{"gateway_api": {"enabled": false}}\n')
    write(tmp_path / "core" / "live_control.json", '{"disabled_providers": ["fake"]}\n')
    write(tmp_path / "providers" / "fake" / "config.json", '{"base_url": "https://new"}\n')
    write(tmp_path / "providers" / "fake" / "secrets.json", '{"api_key": "secret"}\n')
    assert inspector.restart_required()["required"] is False

    write(tmp_path / ".env", "PORT=8000\n")
    write(tmp_path / "api" / "server.py", "APP_VERSION = 2\n")
    write(tmp_path / "providers" / "fake" / "manifest.json", '{"models": {}}\n')
    status = inspector.restart_required()
    assert status["required"] is True
    assert status["message"] == "检测到需要重启的变量，请重启"
    assert status["changed_groups"] == ["environment", "backend", "providers"]


def test_version_check_compares_local_and_remote_versions(tmp_path: Path, monkeypatch) -> None:
    write(
        tmp_path / "version.json",
        json.dumps({"version": "0.4", "protocol_version": "1.0", "notes": "local"}),
    )
    inspector = SystemInspector(tmp_path)
    monkeypatch.setattr(
        inspector,
        "_remote_version",
        lambda: {
            "version": "0.5.0",
            "protocol_version": "1.1",
            "build": None,
            "notes": "remote",
        },
    )

    result = inspector.version_check()
    assert result["status"] == "update_available"
    assert result["update_available"] is True
    assert result["local"]["version"] == "0.4"
    assert result["remote"]["version"] == "0.5.0"
    assert result["source"] == REMOTE_VERSION_URL


def test_version_check_reports_network_failure_without_claiming_latest(
    tmp_path: Path, monkeypatch
) -> None:
    write(tmp_path / "version.json", '{"version": "0.4.0"}\n')
    inspector = SystemInspector(tmp_path)

    def unavailable():
        raise URLError("offline")

    monkeypatch.setattr(inspector, "_remote_version", unavailable)
    result = inspector.version_check()
    assert result["status"] == "unavailable"
    assert result["update_available"] is None
    assert result["remote"] is None
    assert "最新" not in result["message"]
