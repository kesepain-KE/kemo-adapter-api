from __future__ import annotations

import os
from pathlib import Path

import start_web


STARTUP_ENV_NAMES = (
    "HOST",
    "PORT",
    "LOG_LEVEL",
    "WEB_ACCESS_LOG",
    "WEB_OPEN_BROWSER",
    "GATEWAY_API_KEYS_JSON",
    "GATEWAY_API_KEY",
    "PROVIDER_SETTINGS_JSON",
)


def clear_startup_env(monkeypatch) -> None:
    for name in STARTUP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_start_web_loads_project_env_and_process_values_take_priority(
    tmp_path: Path, monkeypatch
) -> None:
    clear_startup_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HOST=127.0.0.9\nPORT=9876\nLOG_LEVEL=debug\n"
        "WEB_ACCESS_LOG=false\nWEB_OPEN_BROWSER=false\n",
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(start_web, "ENV_FILE", env_file)
    monkeypatch.setattr(start_web, "FRONTEND_DIST", dist)
    monkeypatch.setenv("HOST", "127.0.0.8")

    def load_test_env(path: Path, *, override: bool) -> bool:
        assert path == env_file
        assert override is False
        values = {
            "HOST": "127.0.0.9",
            "PORT": "9876",
            "LOG_LEVEL": "debug",
            "WEB_ACCESS_LOG": "false",
            "WEB_OPEN_BROWSER": "false",
        }
        for name, value in values.items():
            if name not in os.environ:
                monkeypatch.setenv(name, value)
        return True

    monkeypatch.setattr(start_web, "load_dotenv", load_test_env)

    captured: dict = {}

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            captured.update(kwargs)

    class FakeServer:
        started = True
        should_exit = False

        def __init__(self, config) -> None:
            self.config = config

        def run(self) -> None:
            return None

    monkeypatch.setattr(start_web.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(start_web.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(start_web, "write_pid_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(start_web, "clear_pid_metadata", lambda *args, **kwargs: None)

    assert start_web.main() == 0
    assert captured["host"] == "127.0.0.8"
    assert captured["port"] == 9876
    assert captured["log_level"] == "debug"
    assert captured["access_log"] is False
    assert captured["workers"] == 1


def test_start_web_rejects_invalid_environment_without_echoing_value(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    clear_startup_env(monkeypatch)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(start_web, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(start_web, "FRONTEND_DIST", dist)
    monkeypatch.setattr(start_web, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("PORT", "not-a-port-secret")
    called = False

    def runner(*args, **kwargs) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(start_web.uvicorn, "run", runner)

    assert start_web.main() == 2
    output = capsys.readouterr()
    assert "not-a-port-secret" not in output.err
    assert called is False


def test_start_web_requires_built_frontend(tmp_path: Path, monkeypatch, capsys) -> None:
    clear_startup_env(monkeypatch)
    monkeypatch.setattr(start_web, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(start_web, "FRONTEND_DIST", tmp_path / "missing-dist")
    monkeypatch.setattr(start_web, "load_dotenv", lambda *args, **kwargs: False)

    assert start_web.main() == 2
    assert "pnpm run build" in capsys.readouterr().err


def test_browser_url_uses_loopback_for_wildcard_bindings() -> None:
    assert start_web._browser_url("0.0.0.0", 8741) == "http://127.0.0.1:8741/admin"
    assert start_web._browser_url("::", 8741) == "http://127.0.0.1:8741/admin"
