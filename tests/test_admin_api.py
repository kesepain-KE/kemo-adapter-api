from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from tests.test_live_config import project, write_json


ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
CALLER_HEADERS = {"Authorization": "Bearer caller-token"}


def admin_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_json(
        root / "api" / "keys.json",
        {
            "keys": {
                "admin-token": {
                    "tenant_id": "admin",
                    "subject_id": "console",
                    "scopes": ["admin:web"],
                },
                "owner-token": {
                    "tenant_id": "admin",
                    "subject_id": "owner-console",
                    "scopes": ["owner"],
                },
                "caller-token": {
                    "tenant_id": "tenant",
                    "subject_id": "agent",
                    "scopes": ["model:invoke"],
                },
            }
        },
    )
    return root


def no_auth_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_json(root / "api" / "keys.json", {"keys": {}})
    return root


def test_empty_auth_environment_allows_direct_web_owner_but_not_public_api(
    tmp_path: Path, monkeypatch
) -> None:
    root = no_auth_project(tmp_path)
    for name in ("GATEWAY_API_KEY", "GATEWAY_API_KEYS_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEB_USERNAME", "")
    monkeypatch.setenv("WEB_PASSWORD", "")
    app = create_app(
        Settings.from_env(), live_config_root=root, discover_providers=False
    )

    with TestClient(app) as client:
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["authentication"] == {"required": False}
        assert console.json()["permissions"] == {"can_restart": True}
        assert client.get("/admin/api/system/restart").status_code == 200
        assert client.get(
            "/model/capabilities", params={"model": "unknown-model"}
        ).status_code == 401


def test_any_configured_auth_value_disables_direct_web_login(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    token_settings = Settings(
        api_keys={
            "configured-token": PrincipalConfig(
                "tenant", "subject", frozenset({"owner"})
            )
        }
    )
    user_settings = Settings(web_username="admin", web_password="password")

    for settings in (token_settings, user_settings):
        app = create_app(
            settings, live_config_root=root, discover_providers=False
        )
        with TestClient(app) as client:
            assert client.get("/admin/api/console").status_code == 401


def test_public_api_token_does_not_disable_direct_web_login(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    settings = Settings(
        api_keys={
            "model-token": PrincipalConfig(
                "tenant", "agent", frozenset({"model:invoke"})
            )
        }
    )
    app = create_app(settings, live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        response = client.get("/admin/api/console")
        assert response.status_code == 200
        assert response.json()["authentication"] == {"required": False}


def test_admin_console_requires_admin_scope_and_redacts_provider_secrets(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    write_json(
        root / "providers" / "fake" / "config.json",
        {
            "base_url": "https://provider.invalid",
            "api_key": "must-not-reach-browser",
            "nested": {"access_token": "also-hidden", "region": "test"},
        },
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        assert client.get("/admin/api/console").status_code == 401
        assert client.get("/admin/api/console", headers=CALLER_HEADERS).status_code == 403

        response = client.get("/admin/api/console", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert response.json()["authentication"] == {"required": True}
        assert response.json()["permissions"] == {"can_restart": False}
        assert client.get(
            "/admin/api/console",
            headers={"Authorization": "Bearer owner-token"},
        ).json()["permissions"] == {"can_restart": True}
        config = response.json()["provider_configs"]["fake"]
        assert config == {
            "base_url": "https://provider.invalid",
            "nested": {"region": "test"},
        }
        assert "must-not-reach-browser" not in response.text
        assert "also-hidden" not in response.text


def test_revision_conflict_does_not_replace_runtime_config(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        response = client.put(
            "/admin/api/runtime/gateway",
            headers=ADMIN_HEADERS,
            json={"expected_revision": "stale-revision", "enabled": False},
        )

    assert response.status_code == 409
    runtime = json.loads((root / "api" / "runtime.json").read_text(encoding="utf-8"))
    assert runtime == {"gateway_api": {"enabled": True}}


def test_admin_can_reenable_gateway_after_public_api_is_disabled(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get("/admin/api/console", headers=ADMIN_HEADERS).json()["revision"]
        disabled = client.put(
            "/admin/api/runtime/gateway",
            headers=ADMIN_HEADERS,
            json={"expected_revision": revision, "enabled": False},
        )
        assert disabled.status_code == 200

        public = client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
            headers=CALLER_HEADERS,
        )
        assert public.status_code == 503
        assert client.get("/admin/api/console", headers=ADMIN_HEADERS).status_code == 200

        enabled = client.put(
            "/admin/api/runtime/gateway",
            headers=ADMIN_HEADERS,
            json={
                "expected_revision": disabled.json()["revision"],
                "enabled": True,
            },
        )
        assert enabled.status_code == 200
        restored = client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
            headers=CALLER_HEADERS,
        )
        assert restored.status_code == 404


def test_provider_api_key_is_write_only_and_hot_updated(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get("/admin/api/console", headers=ADMIN_HEADERS).json()["revision"]
        updated = client.put(
            "/admin/api/runtime/providers/fake",
            headers=ADMIN_HEADERS,
            json={
                "expected_revision": revision,
                "config": {"base_url": "https://new.invalid", "timeout_seconds": 30},
                "api_key": "new-provider-secret",
            },
        )
        assert updated.status_code == 200
        console = client.get("/admin/api/console", headers=ADMIN_HEADERS)
        assert console.json()["provider_configs"]["fake"] == {
            "base_url": "https://new.invalid",
            "timeout_seconds": 30,
        }
        assert "new-provider-secret" not in console.text

    secrets = json.loads((root / "providers" / "fake" / "secrets.json").read_text(encoding="utf-8"))
    assert secrets["api_key"] == "new-provider-secret"
