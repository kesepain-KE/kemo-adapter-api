import os

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.yaml_config import dump_yaml, parse_yaml
from tests.support.admin import ADMIN_HEADERS, CALLER_HEADERS, OWNER_HEADERS, admin_settings
from web.backend.startup_config import EDITABLE, SECRETS, config_view, save_config, StartupConflict


@pytest.fixture(autouse=True)
def clean_validation_environment(monkeypatch):
    for name in EDITABLE | SECRETS | {"GATEWAY_API_KEYS_JSON"}:
        monkeypatch.delenv(name, raising=False)


def test_startup_config_preserves_secrets_unknown_fields_and_live_settings(tmp_path):
    path = tmp_path / "gateway.yaml"
    source = {
        "HOST": "127.0.0.1", "PORT": 7531, "WEB_PASSWORD": "original-private-password",
        "WEB_USERNAME": "operator", "UNKNOWN_PRIVATE": "do-not-return", "LOG_LEVEL": "info",
    }
    path.write_text(dump_yaml(source), encoding="utf-8")
    app = create_app(admin_settings(port=8531), live_config_root=tmp_path)
    with TestClient(app) as client:
        url = "/admin/api/system/startup-config"
        view = client.get(url, headers=OWNER_HEADERS)
        assert view.status_code == 200
        assert "no-store" in view.headers["cache-control"]
        assert source["WEB_PASSWORD"] not in view.text and source["UNKNOWN_PRIVATE"] not in view.text
        assert parse_yaml(view.json()["yaml"])["PORT"] == 7531
        assert "WEB_PASSWORD" not in parse_yaml(view.json()["yaml"])
        values = {"HOST": "127.0.0.1", "PORT": 9531, "WEB_ALLOWED_HOSTS": ["localhost", "127.0.0.1"]}
        response = client.put(url, headers=OWNER_HEADERS, json={
            "expected_revision": view.json()["expected_revision"], "yaml": dump_yaml(values),
        })
        assert response.status_code == 200, response.text
        assert response.json()["restart_required"] is True
        assert response.json()["expected_revision"] != view.json()["expected_revision"]
        saved = parse_yaml(path.read_text(encoding="utf-8"))
        assert saved == {**{k: v for k, v in source.items() if k not in EDITABLE}, **values}
        assert app.state.settings.port == 8531
        assert os.getenv("PORT") is None
        assert client.put(url, headers=OWNER_HEADERS, json={
            "expected_revision": view.json()["expected_revision"], "yaml": "{}",
        }).status_code == 409
        current = response.json()
        response = client.put(url, headers=OWNER_HEADERS, json={
            "expected_revision": current["expected_revision"], "yaml": current["yaml"],
            "secrets": {"WEB_PASSWORD": "replacement-private-password"},
        })
        assert response.status_code == 200
        assert "replacement-private-password" not in response.text
        assert parse_yaml(path.read_text(encoding="utf-8"))["WEB_PASSWORD"] == "replacement-private-password"
        assert any(row[0] == "save-startup-config" for row in app.state.registry.store._db.execute("SELECT action FROM audit"))
    assert not list(tmp_path.glob(".gateway-*.yaml"))


@pytest.mark.parametrize("text", [
    "PORT: 0", "PORT: 70000", "PORT: [7531]", "PORT: 7531\nPORT: 7532",
    "WEB_PASSWORD: forbidden", "KEMO_MASTER_KEY: forbidden", "OTHER: forbidden", "[]", "PORT: [",
    "STATISTICS_TIMEZONE: Mars/Nowhere", "LOG_LEVEL: verbose", "WEB_ACCESS_LOG: invalid",
    "GATEWAY_BASE_URL: https://public.example", "GATEWAY_BASE_URL: ftp://127.0.0.1",
    "GATEWAY_BASE_URL: http://operator:secret@127.0.0.1", "GATEWAY_BASE_URL: http://127.0.0.1:99999",
    "RESTART_DRAIN_TIMEOUT: 0", "SSE_HEARTBEAT_SECONDS: .nan",
])
def test_invalid_startup_config_leaves_original_file_untouched(tmp_path, text):
    path = tmp_path / "gateway.yaml"
    path.write_text("HOST: 127.0.0.1\nPORT: 7531\n", encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save_config(tmp_path, config_view(tmp_path)["expected_revision"], text, {})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".gateway-*.yaml"))


def test_startup_config_owner_csrf_and_explicit_secret_clear(tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text("WEB_TOKEN: old-private-token\n", encoding="utf-8")
    settings = admin_settings(host="0.0.0.0", web_token="session-token")
    app = create_app(settings, live_config_root=tmp_path)
    with TestClient(app) as client:
        url = "/admin/api/system/startup-config"
        assert client.get(url).status_code == 401
        assert client.get(url, headers=CALLER_HEADERS).status_code == 403
        assert client.get(url, headers=ADMIN_HEADERS).status_code == 403
        current = client.get(url, headers=OWNER_HEADERS).json()
        payload = {"expected_revision": current["expected_revision"], "yaml": "HOST: 127.0.0.1\n",
                   "secrets": {"WEB_TOKEN": ""}}
        assert client.put(url, json=payload, headers=ADMIN_HEADERS).status_code == 403
        login = client.post("/admin/api/auth/token", json={"token": "session-token"})
        assert login.status_code == 200
        assert client.put(url, json=payload).status_code == 403
        assert client.put(url, json=payload, headers={
            "X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://foreign.example",
        }).status_code == 403
        response = client.put(url, json=payload, headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert response.status_code == 200, response.text
        assert parse_yaml(path.read_text(encoding="utf-8"))["WEB_TOKEN"] == ""
        assert settings.web_token == "session-token"


def test_startup_config_hidden_url_overrides_and_external_conflict(tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text("PORT: 7531\nGATEWAY_BASE_URL: http://user:private-password@127.0.0.1\n", encoding="utf-8")
    current = config_view(tmp_path, ["PORT", "PRIVATE_ENV"])
    assert "private-password" not in str(current)
    assert "GATEWAY_BASE_URL" not in parse_yaml(current["yaml"])
    assert current["hidden_fields"] == ["GATEWAY_BASE_URL"]
    assert current["environment_overrides"] == ["PORT"]
    path.write_text("PORT: 7532\n", encoding="utf-8")
    with pytest.raises(StartupConflict):
        save_config(tmp_path, current["expected_revision"], "PORT: 7533", {})
    assert path.read_text(encoding="utf-8") == "PORT: 7532\n"
