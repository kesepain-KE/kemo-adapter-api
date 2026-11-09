from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings, PrincipalConfig
from core.secret_preview import mask_secret
from tests.support.admin import admin_project, admin_settings, ADMIN_HEADERS, OWNER_HEADERS, CALLER_HEADERS
from web.backend.gateway_config import gateway_config_view


@pytest.mark.parametrize("headers", [ADMIN_HEADERS, OWNER_HEADERS])
def test_gateway_config_is_read_only_and_never_returns_secrets(tmp_path: Path, headers) -> None:
    root = admin_project(tmp_path)
    secret = "unique-test-credential-do-not-return"
    settings = admin_settings(
        host="0.0.0.0", port=8531, max_concurrent_executions=77,
        web_username=secret, web_password=secret, web_token=secret,
        status_token=secret,
    )
    app = create_app(settings, live_config_root=root, )
    # Do not parse gateway.yaml for display or expose pending values.
    (root / "gateway.yaml").write_text("PORT=9999\nUNKNOWN_SECRET=" + secret, encoding="utf-8")
    before = (root / "gateway.yaml").read_bytes()
    with TestClient(app) as client:
        response = client.get("/admin/api/system/gateway-config", headers=headers)
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        body = response.json()
        assert body["read_only"] is True
        assert body["source"] == "running_process"
        fields = {item["name"]: item for group in body["groups"] for item in group["items"]}
        assert fields["PORT"]["value"] == "8531"
        assert fields["MAX_CONCURRENT_EXECUTIONS"]["value"] == "77"
        assert fields["LOG_RETENTION_DAYS"]["value"] == "30"
        for name in ("WEB_USERNAME", "WEB_PASSWORD", "WEB_TOKEN", "STATUS_TOKEN"):
            assert fields[name]["value"] == "uniqu…urn"
        assert fields["GATEWAY_API_KEY"]["sensitive"] is True
        assert all(item["sensitive"] for name, item in fields.items() if name.startswith("WEB_") and name not in {"WEB_ALLOWED_HOSTS", "WEB_COOKIE_SECURE"})
        for value in (secret, "private-provider", "UNKNOWN_SECRET"):
            assert value not in response.text
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            result = client.request(method, "/admin/api/system/gateway-config", headers=headers, json={"PORT": 9999})
            assert result.status_code == 405
        assert client.get("/admin/api/system/gateway-config", headers=CALLER_HEADERS).status_code == 403
        assert client.get("/admin/api/system/gateway-config").status_code == 401
    assert (root / "gateway.yaml").read_bytes() == before


def test_log_retention_uses_days_and_ignores_legacy_hours(monkeypatch) -> None:
    monkeypatch.delenv("LOG_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("EXECUTION_RETENTION_HOURS", "24")
    settings = Settings.from_env()
    assert settings.log_retention_days == 30
    assert settings.execution_retention_hours == 30 * 24

    monkeypatch.setenv("LOG_RETENTION_DAYS", "14")
    settings = Settings.from_env()
    assert settings.log_retention_days == 14
    assert settings.execution_retention_hours == 14 * 24


@pytest.mark.parametrize("url", [
    "https://gateway.example/?access_token=secret-value",
    "https://gateway.example/#secret-value",
    "https://name:secret-value@gateway.example/",
])
def test_gateway_config_masks_sensitive_url(url: str) -> None:
    view = gateway_config_view(Settings(base_url=url))
    assert "secret-value" not in str(view)
    fields = {item["name"]: item for group in view["groups"] for item in group["items"]}
    assert fields["GATEWAY_BASE_URL"]["value"] == "***"


def test_gateway_config_empty_credentials_also_use_constant_mask() -> None:
    view = gateway_config_view(Settings())
    assert all(item["value"] == "***" for group in view["groups"] for item in group["items"] if item["sensitive"])


@pytest.mark.parametrize("secret", [None, "", "x", "12345678", "1234567890", {"api_key": "never-serialize-me"}, "secret with spaces", "secret\nmultiline", "secret…ellipsis"])
def test_short_or_structured_secrets_are_fully_hidden(secret) -> None:
    assert mask_secret(secret) == "***"


def test_preview_uses_exact_five_and_three_characters() -> None:
    assert mask_secret("12345678901") == "12345…901"
    assert mask_secret("sk-abcdef0123456789") == "sk-ab…789"


def test_gateway_config_masks_each_bootstrap_key() -> None:
    startup_key = "start-secret-middle-111"
    backup_key = "back1-secret-middle-222"
    view = gateway_config_view(
        Settings(api_keys={
            startup_key: PrincipalConfig("t", "s"),
            backup_key: PrincipalConfig("t", "s"),
        }),
    )
    fields = {item["name"]: item for group in view["groups"] for item in group["items"]}
    assert fields["GATEWAY_API_KEY"]["value"].splitlines() == ["start…111", "back1…222"]
    for secret in (startup_key, backup_key):
        assert secret not in str(view)
