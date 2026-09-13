from __future__ import annotations

from tests.support.admin import ADMIN_HEADERS, OWNER_HEADERS, CALLER_HEADERS, admin_project

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.provider_contract import ProviderProbeResult
from tests.support.project import project, write_json
from tests.support.llm import FakeProvider
from tests.support.llm import request as provider_request
from web.backend.service import RuntimeConfigWriter


def no_auth_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_json(root / "api" / "keys.json", {"keys": {}})
    return root


def test_provider_capabilities_are_available_only_through_admin_scope(
    tmp_path: Path,
) -> None:
    probe_models = []

    class InspectingFakeProvider(FakeProvider):
        async def probe(self, model, context):
            del context
            probe_models.append(model)
            return ProviderProbeResult(reachable=True, status="completed")

    root = admin_project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)
    app.state.registry.register(InspectingFakeProvider())

    with TestClient(app) as client:
        response = client.get(
            "/admin/api/providers/fake/capabilities", headers=ADMIN_HEADERS
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["provider_id"] == "fake"
        assert payload["models"][0]["model"] == "fake-model"
        assert payload["models"][0]["streaming"] is True
        assert payload["errors"] == []
        assert client.get(
            "/admin/api/providers/fake/capabilities", headers=CALLER_HEADERS
        ).status_code == 403
        assert client.get(
            "/admin/api/providers/missing/capabilities", headers=ADMIN_HEADERS
        ).status_code == 404
        probe = client.post(
            "/admin/api/models/fake-model/probe", headers=ADMIN_HEADERS
        )
        assert probe.status_code == 200
        assert probe.json()["model"] == "fake-model"
        assert probe.json()["task"] == "llm"
        assert probe.json()["reachable"] is True
        assert probe.json()["status"] == "completed"
        assert probe.json()["latency_ms"] >= 0
        assert probe_models == ["fake-model"]


def test_empty_auth_environment_allows_direct_web_owner_but_not_public_api(
    tmp_path: Path, monkeypatch
) -> None:
    root = no_auth_project(tmp_path)
    for name in ("GATEWAY_API_KEY", "GATEWAY_API_KEYS_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEB_USERNAME", "")
    monkeypatch.setenv("WEB_PASSWORD", "")
    monkeypatch.setenv("WEB_TOKEN", "")
    monkeypatch.setenv("HOST", "127.0.0.1")
    app = create_app(
        Settings.from_env(), live_config_root=root, discover_providers=False
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["authentication"] == {"required": False}
        assert console.json()["permissions"] == {"can_restart": True}
        assert client.get("/admin/api/system/restart").status_code == 200
        assert client.get(
            "/model/capabilities", params={"model": "unknown-model"}
        ).status_code == 401


def test_non_loopback_web_console_allows_empty_auth(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(host="0.0.0.0"),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(app) as client:
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["authentication"] == {"required": False}
        assert console.json()["permissions"] == {"can_restart": True}


def test_loopback_web_console_bypasses_stale_web_credentials(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(
            host="127.0.0.1",
            web_token="stale-token",
            web_username="old-user",
            web_password="old-password",
        ),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        methods = client.get("/admin/api/auth/methods")
        console = client.get("/admin/api/console")

    assert methods.status_code == 200
    assert methods.json()["token_required"] is False
    assert methods.json()["password_required"] is False
    assert methods.json()["configuration_valid"] is True
    assert console.status_code == 200


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


def test_web_token_issues_two_hour_control_plane_session_only(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(web_token="web-control-token"),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(app) as client:
        assert client.get("/admin/api/console").status_code == 401
        assert client.get(
            "/admin/api/console",
            headers={"Authorization": "Bearer web-control-token"},
        ).status_code == 401
        session = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
        assert session.status_code == 200
        assert session.json()["next_step"] == "complete"
        assert session.json()["expires_in"] == 7200
        assert "session_token" not in session.json()
        assert session.json()["csrf_token"].startswith("csrf_")
        cookie = session.headers["set-cookie"].lower()
        assert "kemo_web_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["permissions"] == {"can_restart": True}
        assert "script-src 'self'" in console.headers["content-security-policy"]
        assert console.headers["x-frame-options"] == "DENY"
        assert console.headers["referrer-policy"] == "no-referrer"
        assert console.headers["cache-control"] == "no-store, max-age=0"
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/admin/api/system/restart").status_code == 200
        assert client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
        ).status_code == 401


def test_https_public_base_sets_secure_session_cookie(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(
            web_token="web-control-token",
            web_username="kemo",
            web_password="password",
            base_url="https://gateway.example.com",
        ),
        live_config_root=root,
        discover_providers=False,
    )
    with TestClient(app, base_url="https://gateway.example.com") as client:
        response = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_public_base_rejects_missing_web_credentials(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    for settings in (
        Settings(base_url="https://gateway.example.com"),
        Settings(web_token="web-control-token", base_url="https://gateway.example.com"),
        Settings(
            web_token="web-control-token",
            web_username="kemo",
            base_url="https://gateway.example.com",
        ),
    ):
        try:
            create_app(settings, live_config_root=root, discover_providers=False)
        except RuntimeError as exc:
            assert "公网 GATEWAY_BASE_URL" in str(exc)
        else:
            raise AssertionError("public Base URL must require complete Web credentials")


def test_lan_http_entry_does_not_set_secure_cookie_when_public_base_is_https(
    tmp_path: Path,
) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(
            web_token="web-control-token",
            web_username="kemo",
            web_password="password",
            base_url="https://gateway.example.com",
        ),
        live_config_root=root,
        discover_providers=False,
    )
    with TestClient(app, base_url="http://192.168.1.20") as client:
        response = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
        password = client.post(
            "/admin/api/auth/password",
            json={"username": "kemo", "password": "password"},
        )
    assert response.status_code == 200
    assert response.json()["next_step"] == "password"
    assert password.status_code == 200
    assert "secure" not in response.headers["set-cookie"].lower()


def test_auth_validation_response_does_not_echo_secret_input(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(web_token="web-control-token"),
        live_config_root=root,
        discover_providers=False,
    )
    marker = "must-not-be-reflected"
    with TestClient(app) as client:
        response = client.post(
            "/admin/api/auth/token", json={"token": marker * 500}
        )
    assert response.status_code == 422
    assert marker not in response.text


def test_web_token_precedes_password_and_both_sessions_last_two_hours(
    tmp_path: Path,
) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(
            web_token="first-factor",
            web_username="kemo",
            web_password="correct-password",
        ),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(app) as client:
        methods = client.get("/admin/api/auth/methods").json()
        assert methods == {
            "token_required": True,
            "password_required": True,
            "configuration_valid": True,
            "session_ttl_seconds": 7200,
        }
        assert client.post(
            "/admin/api/auth/password",
            json={"username": "kemo", "password": "correct-password"},
        ).status_code == 401

        token_step = client.post(
            "/admin/api/auth/token", json={"token": "first-factor"}
        )
        assert token_step.status_code == 200
        assert token_step.json()["next_step"] == "password"
        assert token_step.json()["expires_in"] == 7200
        assert token_step.json()["csrf_token"] is None
        assert "session_token" not in token_step.json()
        assert client.get("/admin/api/console").status_code == 401

        password_step = client.post(
            "/admin/api/auth/password",
            json={"username": "kemo", "password": "correct-password"},
        )
        assert password_step.status_code == 200
        assert password_step.json()["next_step"] == "complete"
        assert password_step.json()["expires_in"] == 7200
        assert password_step.json()["csrf_token"].startswith("csrf_")
        assert "session_token" not in password_step.json()
        assert client.get("/admin/api/console").status_code == 200
        assert client.get("/admin/api/auth/session").json()["authenticated"] is True


def test_web_password_can_authenticate_without_web_token(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(web_username="kemo", web_password="password"),
        live_config_root=root,
        discover_providers=False,
    )
    with TestClient(app) as client:
        session = client.post(
            "/admin/api/auth/password",
            json={"username": "kemo", "password": "password"},
        )
        assert session.status_code == 200
        assert "session_token" not in session.json()
        assert client.get("/admin/api/console").status_code == 200


def test_cookie_authenticated_admin_writes_require_csrf_and_same_origin(
    tmp_path: Path,
) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(web_token="web-control-token"),
        live_config_root=root,
        discover_providers=False,
    )
    with TestClient(app) as client:
        login = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
        csrf_token = login.json()["csrf_token"]
        revision = client.get("/admin/api/console").json()["revision"]
        body = {"expected_revision": revision, "enabled": False}

        assert client.put("/admin/api/runtime/gateway", json=body).status_code == 403
        assert client.put(
            "/admin/api/runtime/gateway",
            headers={"X-CSRF-Token": csrf_token, "Origin": "https://evil.example"},
            json=body,
        ).status_code == 403
        accepted = client.put(
            "/admin/api/runtime/gateway",
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
            json=body,
        )
        assert accepted.status_code == 200


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
            "default_headers": {
                "Authorization": "Bearer hidden",
                "X-API-Key": "header-secret",
            },
        },
    )
    app = create_app(
        Settings(
            web_token="web-control-token",
            web_username="kemo",
            web_password="password",
            base_url="https://gateway.example.com",
        ),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(app) as client:
        assert client.get("/admin/api/console").status_code == 401
        assert client.get("/admin/api/console", headers=CALLER_HEADERS).status_code == 403

        response = client.get("/admin/api/console", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert response.json()["base_url"] == "https://gateway.example.com"
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
            "default_headers": {"Authorization": "", "X-API-Key": ""},
        }
        assert "must-not-reach-browser" not in response.text
        assert "also-hidden" not in response.text
        assert "Bearer hidden" not in response.text
    assert "header-secret" not in response.text


def test_admin_console_allowlists_provider_diagnostics(tmp_path: Path) -> None:
    class LeakyDiagnosticsProvider(FakeProvider):
        def diagnostics(self):
            return {
                "provider_id": self.provider_id,
                "models": sorted(self.models),
                "api_key": "diagnostic-secret",
                "headers": {"Authorization": "Bearer diagnostic-secret"},
                "nested": {"password": "nested-secret"},
            }

        def key_statuses(self):
            return [
                {
                    "key_id": "primary",
                    "key_preview": "diag…safe",
                    "status": "healthy",
                    "calls": 1,
                    "unexpected_secret": "do-not-forward",
                }
            ]

    root = admin_project(tmp_path)
    app = create_app(
        Settings(
            web_token="web-control-token",
            web_username="kemo",
            web_password="password",
            base_url="https://gateway.example.com",
        ),
        live_config_root=root,
        discover_providers=False,
    )
    app.state.registry.register(LeakyDiagnosticsProvider())

    with TestClient(app) as client:
        response = client.get("/admin/api/console", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert set(provider) == {"provider_id", "models", "key_statuses"}
    assert "diagnostic-secret" not in response.text
    assert "nested-secret" not in response.text
    assert "unexpected_secret" not in response.text


def test_console_survives_provider_key_diagnostics_failure(tmp_path: Path) -> None:
    class BrokenKeyDiagnosticsProvider(FakeProvider):
        def key_statuses(self):
            raise RuntimeError("stale provider package")

    root = admin_project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)
    app.state.registry.register(BrokenKeyDiagnosticsProvider())

    with TestClient(app) as client:
        response = client.get("/admin/api/console", headers=ADMIN_HEADERS)
        key_statuses = client.get(
            "/admin/api/providers/fake/keys", headers=OWNER_HEADERS
        )

    assert response.status_code == 200
    assert response.json()["providers"][0]["key_statuses"] == []
    assert response.json()["providers"][0]["key_statuses_status"] == "unavailable"
    assert key_statuses.status_code == 200
    assert key_statuses.json()["keys"] == []
    assert key_statuses.json()["key_statuses_status"] == "unavailable"


def test_system_inspection_endpoints_require_admin_scope(tmp_path: Path, monkeypatch) -> None:
    root = admin_project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)
    monkeypatch.setattr(
        app.state.system_inspector,
        "version_check",
        lambda: {
            "status": "up_to_date",
            "update_available": False,
            "local": {"version": "0.4.0"},
            "remote": {"version": "0.4.0"},
            "source": "test",
            "checked_at": "2026-07-27T00:00:00+00:00",
            "message": "当前已是最新版本",
        },
    )

    with TestClient(app) as client:
        assert client.get("/admin/api/system/restart-required").status_code == 401
        assert client.get(
            "/admin/api/system/restart-required", headers=CALLER_HEADERS
        ).status_code == 403
        restart_status = client.get(
            "/admin/api/system/restart-required", headers=ADMIN_HEADERS
        )
        version_status = client.get(
            "/admin/api/system/version-check", headers=ADMIN_HEADERS
        )

    assert restart_status.status_code == 200
    assert restart_status.json()["required"] is False
    assert version_status.status_code == 200
    assert version_status.json()["status"] == "up_to_date"


def test_gateway_keys_are_owner_only_uncached_and_include_real_metadata(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    keys_path = root / "api" / "keys.json"
    payload = json.loads(keys_path.read_text(encoding="utf-8"))
    payload["keys"]["caller-token"].update(
        {"key_id": "graph-production", "created_at": "2026-07-27T10:30:00+08:00"}
    )
    write_json(keys_path, payload)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        assert client.get("/admin/api/keys", headers=ADMIN_HEADERS).status_code == 403
        response = client.get(
            "/admin/api/keys",
            headers={"Authorization": "Bearer owner-token"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    items = {item["name"]: item for item in response.json()["items"]}
    assert "token" not in items["graph-production"]
    assert items["graph-production"]["masked_token"].endswith("oken")
    assert "caller-token" not in response.text
    assert items["graph-production"]["id"] == "graph-production"
    assert items["graph-production"]["created_at"] == "2026-07-27T10:30:00+08:00"
    assert items["graph-production"]["usage"] == {
        "calls": 0,
        "successes": 0,
        "total_tokens": None,
    }
    assert items["graph-production"]["last_used_at"] is None
    assert "owner-console" in items


def test_owner_can_reveal_one_gateway_key_without_exposing_it_in_the_list(
    tmp_path: Path,
) -> None:
    root = admin_project(tmp_path)
    keys_path = root / "api" / "keys.json"
    payload = json.loads(keys_path.read_text(encoding="utf-8"))
    payload["keys"]["caller-token"]["key_id"] = "graph-production"
    write_json(keys_path, payload)
    settings = Settings(
        api_keys={
            "startup-secret": PrincipalConfig(
                "admin",
                "startup-owner",
                frozenset({"owner"}),
                key_id="startup-key",
            )
        }
    )
    app = create_app(settings, live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        owner_headers = {"Authorization": "Bearer owner-token"}
        listed = client.get("/admin/api/keys", headers=owner_headers)
        runtime = client.post(
            "/admin/api/keys/graph-production/reveal", headers=owner_headers
        )
        startup = client.post(
            "/admin/api/keys/startup-key/reveal", headers=owner_headers
        )
        forbidden = client.post(
            "/admin/api/keys/graph-production/reveal", headers=ADMIN_HEADERS
        )
        missing = client.post(
            "/admin/api/keys/missing/reveal", headers=owner_headers
        )

    assert listed.status_code == 200
    assert "caller-token" not in listed.text
    assert "startup-secret" not in listed.text
    assert runtime.status_code == 200
    assert runtime.json() == {"token": "caller-token"}
    assert runtime.headers["cache-control"] == "no-store, max-age=0"
    assert startup.status_code == 200
    assert startup.json() == {"token": "startup-secret"}
    assert forbidden.status_code == 403
    assert missing.status_code == 404


def test_cookie_authenticated_key_reveal_requires_csrf(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    app = create_app(
        Settings(web_token="web-control-token"),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(app) as client:
        login = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
        csrf_token = login.json()["csrf_token"]
        listed = client.get("/admin/api/keys")
        key_id = next(
            item["id"]
            for item in listed.json()["items"]
            if item["name"] == "owner-console"
        )
        assert client.post(f"/admin/api/keys/{key_id}/reveal").status_code == 403
        revealed = client.post(
            f"/admin/api/keys/{key_id}/reveal",
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
        )

    assert revealed.status_code == 200
    assert revealed.json() == {"token": "owner-token"}
    assert revealed.headers["cache-control"] == "no-store, max-age=0"


def test_direct_login_key_reveal_ignores_stale_cookie_but_rejects_cross_site(
    tmp_path: Path,
) -> None:
    root = no_auth_project(tmp_path)
    write_json(
        root / "api" / "keys.json",
        {
            "keys": {
                "local-model-token": {
                    "key_id": "local-agent",
                    "tenant_id": "local",
                    "subject_id": "agent",
                    "scopes": ["model:invoke"],
                }
            }
        },
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.cookies.set("kemo_web_session", "obsolete-session")
        revealed = client.post("/admin/api/keys/local-agent/reveal")
        cross_site = client.post(
            "/admin/api/keys/local-agent/reveal",
            headers={"Origin": "https://evil.example"},
        )

    assert revealed.status_code == 200
    assert revealed.json() == {"token": "local-model-token"}
    assert cross_site.status_code == 403


def test_owner_can_hot_update_key_model_whitelist_and_it_blocks_llm_calls(
    tmp_path: Path,
) -> None:
    root = admin_project(tmp_path)
    keys_path = root / "api" / "keys.json"
    payload = json.loads(keys_path.read_text(encoding="utf-8"))
    payload["keys"]["caller-token"]["key_id"] = "graph-production"
    write_json(keys_path, payload)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)
    app.state.registry.register(FakeProvider())
    owner_headers = {"Authorization": "Bearer owner-token"}

    with TestClient(app) as client:
        listed = client.get("/admin/api/keys", headers=owner_headers)
        assert listed.status_code == 200
        assert listed.json()["models"] == [
            {
                "id": "fake-model",
                "provider_id": "fake",
                "provider_model": "model",
                "enabled": True,
            }
        ]
        caller = next(
            item for item in listed.json()["items"] if item["id"] == "graph-production"
        )
        assert caller["allowed_models"] is None
        assert caller["model_policy"] == "allow_all"

        deny = client.put(
            "/admin/api/keys/graph-production/model-policy",
            headers=owner_headers,
            json={
                "expected_revision": listed.json()["revision"],
                "allowed_models": [],
            },
        )
        assert deny.status_code == 200
        assert deny.json()["model_policy"] == "deny_all"

        body = provider_request(stream=False).model_dump(mode="json")
        blocked = client.post(
            "/model/responses",
            headers={
                **CALLER_HEADERS,
                "X-Kemo-Protocol-Version": "1.0",
                "Idempotency-Key": "req_1",
            },
            json=body,
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "MODEL_NOT_ALLOWED"

        allow = client.put(
            "/admin/api/keys/graph-production/model-policy",
            headers=owner_headers,
            json={
                "expected_revision": deny.json()["revision"],
                "allowed_models": ["fake-model"],
            },
        )
        assert allow.status_code == 200
        assert client.get(
            "/model/capabilities",
            params={"model": "fake-model"},
            headers=CALLER_HEADERS,
        ).status_code == 200

        stale = client.put(
            "/admin/api/keys/graph-production/model-policy",
            headers=owner_headers,
            json={"expected_revision": "stale", "allowed_models": None},
        )
        assert stale.status_code == 409

    stored = json.loads(keys_path.read_text(encoding="utf-8"))
    assert stored["keys"]["caller-token"]["allowed_models"] == ["fake-model"]


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
    env_path = root / ".env"
    env_path.write_text("PROVIDER_SETTINGS_JSON=must-stay-out-of-provider-storage\n", encoding="utf-8")
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
    assert secrets["api_keys"] == [
        {"key_id": "primary", "api_key": "new-provider-secret", "enabled": True}
    ]
    assert "api_key" not in secrets
    assert "api_key_id" not in secrets
    assert env_path.read_text(encoding="utf-8") == "PROVIDER_SETTINGS_JSON=must-stay-out-of-provider-storage\n"


def test_provider_update_validates_secrets_before_writing_config(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    config_path = root / "providers" / "fake" / "config.json"
    secrets_path = root / "providers" / "fake" / "secrets.json"
    original_config = config_path.read_bytes()
    secrets_path.write_text("{invalid", encoding="utf-8")

    writer = RuntimeConfigWriter(root)
    with pytest.raises(json.JSONDecodeError):
        writer.update_provider(
            "fake",
            config={"base_url": "https://new.invalid"},
            api_key="replacement-secret",
        )

    assert config_path.read_bytes() == original_config


def test_provider_key_update_rolls_back_when_hot_reload_fails(tmp_path: Path) -> None:
    class FailingReloadProvider(FakeProvider):
        async def reload_config(self, settings) -> None:
            del settings
            raise ValueError("provider candidate rejected")

    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    original = {
        "api_keys": [
            {"key_id": "primary", "api_key": "first-secret", "enabled": True}
        ]
    }
    write_json(secrets_path, original)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        # Register after startup so the intentionally failing reload is
        # exercised by the mutation, not during application bootstrap.
        app.state.registry.register(FailingReloadProvider())
        revision = client.get(
            "/admin/api/console", headers={"Authorization": "Bearer owner-token"}
        ).json()["revision"]
        response = client.put(
            "/admin/api/providers/fake/keys",
            headers={"Authorization": "Bearer owner-token"},
            json={
                "expected_revision": revision,
                "keys": [
                    {
                        "key_id": "primary",
                        "api_key": "replacement-secret",
                        "enabled": True,
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert json.loads(secrets_path.read_text(encoding="utf-8")) == original


def test_provider_config_update_rolls_back_when_live_file_is_rejected(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    config_path = root / "providers" / "fake" / "config.json"
    secrets_path = root / "providers" / "fake" / "secrets.json"
    original_config = config_path.read_bytes()
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get(
            "/admin/api/console", headers={"Authorization": "Bearer admin-token"}
        ).json()["revision"]
        # The writer can update config.json, but the complete live snapshot
        # must reject a malformed sibling secrets.json and restore the config
        # bytes.
        secrets_path.write_text("{invalid", encoding="utf-8")
        response = client.put(
            "/admin/api/runtime/providers/fake",
            headers={"Authorization": "Bearer admin-token"},
            json={
                "expected_revision": revision,
                "config": {"base_url": "https://candidate.invalid"},
            },
        )

    assert response.status_code == 400
    assert config_path.read_bytes() == original_config


def test_provider_key_append_preserves_legacy_pool_and_hides_secret(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(secrets_path, {"api_key": "legacy-upstream-secret", "api_key_id": "primary"})
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get("/admin/api/console", headers={"Authorization": "Bearer owner-token"}).json()["revision"]
        response = client.post(
            "/admin/api/providers/fake/keys",
            headers={"Authorization": "Bearer owner-token"},
            json={"expected_revision": revision, "api_key": "backup-upstream-secret"},
        )

    assert response.status_code == 200
    assert "backup-upstream-secret" not in response.text
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["api_keys"] == [
        {"key_id": "primary", "api_key": "legacy-upstream-secret", "enabled": True},
        {"key_id": "backup-1", "api_key": "backup-upstream-secret", "enabled": True},
    ]
    assert "api_key" not in stored
    assert "api_key_id" not in stored


def test_provider_key_pool_replace_writes_only_canonical_api_keys(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(
        secrets_path,
        {
            "api_key": "legacy-secret",
            "api_key_id": "legacy-id",
            "provider_specific_secret": "keep-me",
        },
    )

    RuntimeConfigWriter(root).update_provider_keys(
        "fake",
        keys=[
            {"key_id": "primary", "api_key": "first-secret", "enabled": True},
            {"key_id": "backup-1", "api_key": "second-secret", "enabled": False},
        ],
    )

    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["api_keys"] == [
        {"key_id": "primary", "api_key": "first-secret", "enabled": True},
        {"key_id": "backup-1", "api_key": "second-secret", "enabled": False},
    ]
    assert stored["provider_specific_secret"] == "keep-me"
    assert "api_key" not in stored
    assert "api_key_id" not in stored


def test_provider_key_pool_rejects_all_disabled_entries(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    original = {
        "api_keys": [{"key_id": "primary", "api_key": "first-secret", "enabled": True}],
    }
    write_json(secrets_path, original)

    writer = RuntimeConfigWriter(root)
    try:
        writer.update_provider_keys(
            "fake",
            keys=[{"key_id": "primary", "api_key": "first-secret", "enabled": False}],
        )
    except ValueError as exc:
        assert "至少保留一个启用" in str(exc)
    else:
        raise AssertionError("an all-disabled Provider key pool must be rejected")

    assert json.loads(secrets_path.read_text(encoding="utf-8")) == original


def test_provider_key_append_uses_next_available_backup_id(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(
        secrets_path,
        {
            "api_keys": [
                {"key_id": "primary", "api_key": "first-secret", "enabled": True},
                {"key_id": "backup-1", "api_key": "second-secret", "enabled": False},
            ]
        },
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get("/admin/api/console", headers={"Authorization": "Bearer owner-token"}).json()["revision"]
        response = client.post(
            "/admin/api/providers/fake/keys",
            headers={"Authorization": "Bearer owner-token"},
            json={"expected_revision": revision, "api_key": "third-secret"},
        )

    assert response.status_code == 200
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["api_keys"][-1] == {
        "key_id": "backup-2",
        "api_key": "third-secret",
        "enabled": True,
    }


def test_provider_key_delete_removes_non_final_key_and_hides_secret(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(
        secrets_path,
        {
            "api_keys": [
                {"key_id": "primary", "api_key": "first-secret", "enabled": True},
                {"key_id": "backup-1", "api_key": "second-secret", "enabled": True},
            ],
            "provider_specific_secret": "keep-me",
        },
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get(
            "/admin/api/console", headers={"Authorization": "Bearer owner-token"}
        ).json()["revision"]
        response = client.request(
            "DELETE",
            "/admin/api/providers/fake/keys/backup-1",
            headers={"Authorization": "Bearer owner-token"},
            json={"expected_revision": revision},
        )

    assert response.status_code == 200
    assert "first-secret" not in response.text
    assert "second-secret" not in response.text
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["api_keys"] == [
        {"key_id": "primary", "api_key": "first-secret", "enabled": True}
    ]
    assert stored["provider_specific_secret"] == "keep-me"
    assert "api_key" not in stored
    assert "api_key_id" not in stored


def test_provider_key_delete_rejects_final_key_without_leaking_secret(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(
        secrets_path,
        {"api_keys": [{"key_id": "primary", "api_key": "final-secret", "enabled": True}]},
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get(
            "/admin/api/console", headers={"Authorization": "Bearer owner-token"}
        ).json()["revision"]
        response = client.request(
            "DELETE",
            "/admin/api/providers/fake/keys/primary",
            headers={"Authorization": "Bearer owner-token"},
            json={"expected_revision": revision},
        )

    assert response.status_code == 409
    assert "至少保留一个上游密钥" in response.text
    assert "final-secret" not in response.text
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["api_keys"] == [
        {"key_id": "primary", "api_key": "final-secret", "enabled": True}
    ]


def test_provider_key_delete_missing_key_returns_404(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(
        secrets_path,
        {
            "api_keys": [
                {"key_id": "primary", "api_key": "first-secret", "enabled": True},
                {"key_id": "backup-1", "api_key": "second-secret", "enabled": True},
            ]
        },
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        revision = client.get(
            "/admin/api/console", headers={"Authorization": "Bearer owner-token"}
        ).json()["revision"]
        response = client.request(
            "DELETE",
            "/admin/api/providers/fake/keys/not-exists",
            headers={"Authorization": "Bearer owner-token"},
            json={"expected_revision": revision},
        )

    assert response.status_code == 404
    assert "first-secret" not in response.text
    assert "second-secret" not in response.text


def test_provider_key_delete_keeps_at_least_one_entry(tmp_path: Path) -> None:
    root = admin_project(tmp_path)
    secrets_path = root / "providers" / "fake" / "secrets.json"
    write_json(
        secrets_path,
        {
            "api_keys": [
                {"key_id": "primary", "api_key": "first-secret", "enabled": True},
                {"key_id": "backup-1", "api_key": "second-secret", "enabled": True},
            ]
        },
    )
    app = create_app(Settings(), live_config_root=root, discover_providers=False)

    with TestClient(app) as client:
        owner = {"Authorization": "Bearer owner-token"}
        revision = client.get("/admin/api/console", headers=owner).json()["revision"]
        removed = client.request(
            "DELETE",
            "/admin/api/providers/fake/keys/backup-1",
            headers=owner,
            json={"expected_revision": revision},
        )
        assert removed.status_code == 200
        last_revision = removed.json()["revision"]
        blocked = client.request(
            "DELETE",
            "/admin/api/providers/fake/keys/primary",
            headers=owner,
            json={"expected_revision": last_revision},
        )

    assert blocked.status_code == 409
    assert "至少保留一个" in blocked.text
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert stored["api_keys"] == [
        {"key_id": "primary", "api_key": "first-secret", "enabled": True}
    ]
