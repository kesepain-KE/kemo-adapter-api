from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.provider_contract import ProviderProbeResult
from tests.test_live_config import project, write_json
from tests.test_provider_boundary import FakeProvider
from tests.test_provider_boundary import request as provider_request


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

    with TestClient(app) as client:
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
        Settings(web_token="web-control-token", base_url="https://gateway.example.com"),
        live_config_root=root,
        discover_providers=False,
    )
    with TestClient(app) as client:
        response = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


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
        Settings(base_url="https://gateway.example.com"),
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

    with TestClient(app) as client:
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
