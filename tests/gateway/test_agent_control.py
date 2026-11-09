"""Agent operations use the real management boundary with offline upstreams."""
from __future__ import annotations

import json

import httpx

from tests.support.admin import admin_settings, ADMIN_HEADERS, CALLER_HEADERS, OWNER_HEADERS
from tests.support.gateway import SECRET, add_upstream, instance, mutate, register, state


def test_agent_cookie_session_onboards_provider_and_selects_client_models(tmp_path, monkeypatch):
    calls = []

    def upstream(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET", "Declared registration must not generate or probe"
        return httpx.Response(200, json={"data": [{"id": "remote"}]})

    settings = admin_settings(
        host="0.0.0.0", web_token="offline-web-token",
        web_username="operator", web_password="offline-password",
    )
    with instance(tmp_path, monkeypatch, upstream, settings=settings) as (client, _):
        assert client.get("/admin/api/console").status_code == 401
        login = client.post("/admin/api/auth/token", json={"token": "offline-web-token"})
        assert login.status_code == 200 and login.json()["next_step"] == "password"
        assert client.get("/admin/api/console").status_code == 401
        login = client.post("/admin/api/auth/password", json={
            "username": "operator", "password": "offline-password",
        })
        assert login.status_code == 200
        session = client.get("/admin/api/auth/session")
        assert session.status_code == 200
        client.headers["X-CSRF-Token"] = session.json()["csrf_token"]
        assert state(client)["permissions"]["can_manage_keys"] is True

        add_upstream(client)
        discovery = client.post("/admin/api/upstreams/local/discover")
        assert discovery.status_code == 200 and discovery.json()["models"] == ["remote"]
        assert state(client)["models"] == []
        register(client, model="assistant", capabilities={"streaming": False})
        register(client, model="private-model", capabilities={"streaming": False})
        created = mutate(client, "/keys", {
            "name": "Agent workstation", "allowed_models": ["assistant"], "allowed_ops": ["llm"],
        }, key=True)
        assert created.status_code == 200
        identity, token = created.json()["id"], created.json()["token"]
        caller = {"Authorization": f"Bearer {token}", "X-Kemo-Protocol-Version": "2.0"}

        def visible():
            response = client.get("/model/models", params={"task": "llm"}, headers=caller)
            assert response.status_code == 200, response.text
            return {item["id"] for item in response.json()["data"]}

        assert visible() == {"assistant"}
        capabilities = client.get("/model/capabilities", params={"model": "assistant"}, headers=caller)
        assert capabilities.status_code == 200 and capabilities.json()["streaming"] is False
        assert client.get("/admin/api/console", headers=caller).status_code == 403
        before = state(client)
        assert token not in json.dumps(before) and SECRET not in json.dumps(before)

        # A model rename must preserve the client's restriction and update its route.
        changed = mutate(client, "/models/assistant", {"id": "daily-assistant"}, method="PATCH")
        assert changed.status_code == 200
        assert visible() == {"daily-assistant"}
        after = state(client)
        assert next(key for key in after["keys"] if key["id"] == identity)["allowed_models"] == ["daily-assistant"]
        assert {route["public_model"] for route in after["deployments"]} == {"daily-assistant", "private-model"}
        assert after["key_revision"] != before["key_revision"]
        for policy, expected in (
            ({"allowed_models": []}, set()),
            ({"allowed_models": None}, {"daily-assistant", "private-model"}),
            ({"allowed_models": ["daily-assistant"], "allowed_ops": []}, set()),
            ({"allowed_ops": ["llm"]}, {"daily-assistant"}),
        ):
            response = mutate(client, f"/keys/{identity}", policy, method="PATCH", key=True)
            assert response.status_code == 200, response.text
            assert visible() == expected
        assert mutate(client, f"/keys/{identity}", {"enabled": False}, method="PATCH", key=True).status_code == 200
        assert client.get("/model/models", headers=caller).status_code == 401
        assert mutate(client, f"/keys/{identity}", {"enabled": True}, method="PATCH", key=True).status_code == 200
        assert visible() == {"daily-assistant"}
        assert state(client)["attempts"] == []
        assert calls == [("GET", "/local/models")]


def test_upstream_pool_edits_preserve_omitted_secrets_and_survive_reopen(tmp_path, monkeypatch):
    calls = []
    with instance(tmp_path, monkeypatch, lambda request: calls.append(request)) as (client, app):
        add_upstream(client)
        response = mutate(client, "/upstreams/local", {"keys": [
            {"id": "primary", "priority": 10},
            {"id": "backup", "value": "offline-backup-secret", "priority": -10},
        ]}, method="PATCH")
        assert response.status_code == 200, response.text
        assert mutate(client, "/upstreams/local", {"name": "Renamed provider"}, method="PATCH").status_code == 200
        saved = app.state.registry.store.catalog().upstreams[0]
        assert {item.id for item in saved.credentials} == {"primary", "backup"}
        assert app.state.registry.vault.decrypt(saved.credentials[0].encrypted, "local:primary") == SECRET
        assert mutate(client, "/upstreams/local", {"keys": [
            {"id": "primary", "value": "offline-rotated-secret", "priority": 10},
            {"id": "backup", "enabled": False},
        ]}, method="PATCH").status_code == 200
        assert mutate(client, "/upstreams/local", {"keys": [{"id": "primary"}]}, method="PATCH").status_code == 200
        before = state(client)
        assert len(before["upstreams"][0]["credentials"]) == 1
        assert calls == []

    with instance(tmp_path, monkeypatch, lambda request: calls.append(request)) as (client, app):
        after = state(client)
        assert after["revision"] == before["revision"]
        assert after["upstreams"] == before["upstreams"]
        saved = app.state.registry.store.catalog().upstreams[0]
        assert saved.name == "Renamed provider"
        assert saved.credentials[0].priority == 10
        assert app.state.registry.vault.decrypt(saved.credentials[0].encrypted, "local:primary") == "offline-rotated-secret"
        assert calls == []


def test_stale_agent_edits_cannot_overwrite_provider_secrets_or_create_keys(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        stale = state(client)
        assert mutate(client, "/upstreams/local", {"note": "Changed in console"}, method="PATCH").status_code == 200
        active = app.state.registry.current
        response = mutate(client, "/upstreams/local", {
            "keys": [{"id": "primary", "value": "stale-agent-secret"}],
        }, method="PATCH", expected=stale["revision"])
        assert response.status_code == 409
        assert app.state.registry.current is active
        saved = app.state.registry.store.catalog().upstreams[0]
        assert saved.note == "Changed in console"
        assert app.state.registry.vault.decrypt(saved.credentials[0].encrypted, "local:primary") == SECRET

        first = mutate(client, "/keys", {"name": "Another operator"}, key=True)
        assert first.status_code == 200
        response = mutate(client, "/keys", {"name": "Stale agent"}, key=True, expected=stale["key_revision"])
        assert response.status_code == 409
        assert [key["name"] for key in state(client)["keys"]] == ["Another operator"]
        assert "stale-agent-secret" not in response.text


def test_management_roles_are_distinct_from_invocation_credentials(tmp_path, monkeypatch):
    settings = admin_settings(host="0.0.0.0")
    with instance(tmp_path, monkeypatch, settings=settings) as (client, _):
        response = client.get("/admin/api/console", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        current = response.json()
        assert current["permissions"]["can_manage_keys"] is False
        response = client.post("/admin/api/upstreams", headers=ADMIN_HEADERS, json={
            "expected_revision": current["revision"], "value": {
                "id": "agent", "name": "Agent service", "adapter_id": "openai-chat",
                "base_url": "http://127.0.0.1:8881", "allow_private_network": True,
                "keys": [{"id": "primary", "value": SECRET}],
            },
        })
        assert response.status_code == 200, response.text
        current = client.get("/admin/api/console", headers=ADMIN_HEADERS).json()
        denied = client.post("/admin/api/keys", headers=ADMIN_HEADERS, json={
            "expected_revision": current["key_revision"], "value": {"name": "Must not exist"},
        })
        assert denied.status_code == 403
        assert client.get("/admin/api/system/startup-config", headers=ADMIN_HEADERS).status_code == 403
        assert client.get("/admin/api/system/restart", headers=ADMIN_HEADERS).status_code == 403
        assert client.get("/admin/api/console", headers=CALLER_HEADERS).status_code == 403
        denied = client.patch("/admin/api/upstreams/agent", headers=CALLER_HEADERS, json={
            "expected_revision": current["revision"], "value": {"enabled": False},
        })
        assert denied.status_code == 403
        final = client.get("/admin/api/console", headers=OWNER_HEADERS)
        assert final.status_code == 200
        assert final.json()["keys"] == [] and final.json()["upstreams"][0]["enabled"] is True
        assert final.json()["revision"] == current["revision"]
