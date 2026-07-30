from __future__ import annotations

import os
import logging
from pathlib import Path

import start_web


STARTUP_ENV_NAMES = (
    "HOST",
    "PORT",
    "LOG_LEVEL",
    "WEB_ACCESS_LOG",
    "WEB_OPEN_BROWSER",
    "WEB_TOKEN",
    "WEB_USERNAME",
    "WEB_PASSWORD",
    "STATUS_TOKEN",
    "GATEWAY_API_KEYS_JSON",
    "GATEWAY_API_KEY",
    "PROVIDER_SETTINGS_JSON",
    "MODEL_EXECUTION_TIMEOUT_SECONDS",
    "MAX_CONCURRENT_EXECUTIONS",
    "SSE_HEARTBEAT_SECONDS",
    "EXECUTION_RETENTION_HOURS",
    "MAX_SSE_EVENTS_PER_RESPONSE",
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
    monkeypatch.setattr(start_web, "_startup_conflict", lambda *args, **kwargs: None)
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


def test_start_web_rejects_reused_status_token(monkeypatch) -> None:
    clear_startup_env(monkeypatch)
    monkeypatch.setenv("STATUS_TOKEN", "same-secret")
    monkeypatch.setenv("WEB_TOKEN", "same-secret")

    try:
        start_web._startup_options()
    except ValueError as exc:
        assert "STATUS_TOKEN" in str(exc)
        assert "same-secret" not in str(exc)
    else:
        raise AssertionError("reused STATUS_TOKEN must be rejected")


def test_startup_options_allow_empty_web_auth_on_wildcard_bind(monkeypatch) -> None:
    clear_startup_env(monkeypatch)
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("WEB_TOKEN", "")
    monkeypatch.setenv("WEB_USERNAME", "")
    monkeypatch.setenv("WEB_PASSWORD", "")

    options = start_web._startup_options()

    assert options["host"] == "0.0.0.0"
    assert options["port"] == 7531


def test_start_web_requires_built_frontend(tmp_path: Path, monkeypatch, capsys) -> None:
    clear_startup_env(monkeypatch)
    monkeypatch.setattr(start_web, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(start_web, "FRONTEND_DIST", tmp_path / "missing-dist")
    monkeypatch.setattr(start_web, "load_dotenv", lambda *args, **kwargs: False)

    assert start_web.main() == 2
    assert "pnpm run build" in capsys.readouterr().err


def test_start_web_refuses_a_second_active_instance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    clear_startup_env(monkeypatch)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(start_web, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(start_web, "FRONTEND_DIST", dist)
    monkeypatch.setattr(start_web, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        start_web,
        "_startup_conflict",
        lambda *args, **kwargs: "active_instance",
    )

    assert start_web.main() == 4
    assert "已有网关实例" in capsys.readouterr().err


def test_browser_url_uses_loopback_for_wildcard_bindings() -> None:
    assert start_web._browser_url("0.0.0.0", 7531) == "http://127.0.0.1:7531"
    assert start_web._browser_url("::", 7531) == "http://[::1]:7531"


def test_access_log_filter_redacts_url_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/?token=must-not-leak&next=1", "1.1", 307),
        None,
    )
    assert start_web._RedactTokenQueryFilter().filter(record) is True
    assert "must-not-leak" not in record.getMessage()
    assert "token=<redacted>" in record.getMessage()
