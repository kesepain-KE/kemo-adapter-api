"""Web authentication, cookie boundaries and CSRF against the new management API."""
from __future__ import annotations
from pathlib import Path
from fastapi.testclient import TestClient
from api.server import create_app
from core.config import PrincipalConfig, Settings
from tests.support.project import project, write_yaml


def no_auth_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_yaml(root / "api" / "keys.yaml", {"keys": {}})
    return root


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
        Settings.from_env(), live_config_root=root, )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["authentication"] == {"required": False}
        assert console.json()["permissions"]["can_restart"] is True
        assert client.get("/admin/api/system/restart").status_code == 200
        assert client.get(
            "/model/capabilities", params={"model": "unknown-model"}
        ).status_code == 401


def test_non_loopback_web_console_allows_empty_auth(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(host="0.0.0.0"),
        live_config_root=root,
        )

    with TestClient(app) as client:
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["authentication"] == {"required": False}
        assert console.json()["permissions"]["can_restart"] is True


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
        )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        methods = client.get("/admin/api/auth/methods")
        console = client.get("/admin/api/console")

    assert methods.status_code == 200
    assert methods.json()["token_required"] is False
    assert methods.json()["password_required"] is False
    assert methods.json()["configuration_valid"] is True
    assert methods.json()["next_step"] == "complete"
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
            settings, live_config_root=root, )
        with TestClient(app) as client:
            assert client.get("/admin/api/console").status_code == 401


def test_web_token_issues_two_hour_control_plane_session_only(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(web_token="web-control-token"),
        live_config_root=root,
        )

    with TestClient(app) as client:
        assert client.get("/admin/api/console").status_code == 401
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "token"
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
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "complete"
        cookie = session.headers["set-cookie"].lower()
        assert "kemo_web_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        console = client.get("/admin/api/console")
        assert console.status_code == 200
        assert console.json()["permissions"]["can_restart"] is True
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
            create_app(settings, live_config_root=root, )
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
        )

    with TestClient(app) as client:
        methods = client.get("/admin/api/auth/methods").json()
        assert methods == {
            "token_required": True,
            "password_required": True,
            "configuration_valid": True,
            "session_ttl_seconds": 7200,
            "next_step": "token",
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
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "password"
        assert client.post(
            "/admin/api/auth/password",
            json={"username": "kemo", "password": "wrong-password"},
        ).status_code == 401
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "password"

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
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "complete"


def test_web_auth_returns_to_token_step_when_preauth_is_revoked(tmp_path: Path) -> None:
    app = create_app(
        Settings(web_token="first-factor", web_username="kemo", web_password="password"),
        live_config_root=no_auth_project(tmp_path),
    )
    with TestClient(app) as client:
        assert client.post(
            "/admin/api/auth/token", json={"token": "first-factor"}
        ).status_code == 200
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "password"
        app.state.web_auth.revoke(client.cookies["kemo_web_preauth"])
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "token"
        assert client.post(
            "/admin/api/auth/password",
            json={"username": "kemo", "password": "password"},
        ).status_code == 401


def test_web_password_can_authenticate_without_web_token(tmp_path: Path) -> None:
    root = no_auth_project(tmp_path)
    app = create_app(
        Settings(web_username="kemo", web_password="password"),
        live_config_root=root,
        )
    with TestClient(app) as client:
        assert client.get("/admin/api/auth/methods").json()["next_step"] == "password"
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
        )
    with TestClient(app) as client:
        login = client.post(
            "/admin/api/auth/token", json={"token": "web-control-token"}
        )
        csrf_token = login.json()["csrf_token"]
        revision = client.get("/admin/api/console").json()["revision"]
        body = {"expected_revision": revision, "value": {"enabled": False}}

        assert client.patch("/admin/api/settings", json=body).status_code == 403
        assert client.patch(
            "/admin/api/settings",
            headers={"X-CSRF-Token": csrf_token, "Origin": "https://evil.example"},
            json=body,
        ).status_code == 403
        accepted = client.patch(
            "/admin/api/settings",
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
    app = create_app(settings, live_config_root=root, )

    with TestClient(app) as client:
        response = client.get("/admin/api/console")
        assert response.status_code == 200
        assert response.json()["authentication"] == {"required": False}
