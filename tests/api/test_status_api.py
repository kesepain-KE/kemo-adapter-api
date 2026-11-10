from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import httpx
import pytest
import yaml

from api.server import create_app
from tests.support.gateway import (
    add_upstream, extension_zip, install_extension, install_fixtures, instance,
    invoke, key, mutate, register, state,
)
from api.routes.status import _safe_log_item, _safe_log_message
from core.config import PrincipalConfig, Settings
from core.models import Usage, UsageMeasurement
from gateway.currency import EXCHANGE_RATES
from gateway.diagnostics import usage_overview
from gateway.resources import BUILTIN_PATHS, SOURCE_ROOT
from tests.support.project import project
from tests.support.llm import FakeProvider


STATUS_HEADERS = {"Authorization": "Bearer status-only-token"}


def seed_statistics(app) -> str:
    async def scenario() -> str:
        await app.state.statistics.initialize()
        completed = await app.state.statistics.begin_invocation(
            task="llm",
            provider_id="fake",
            model="fake-model",
            tenant_id="must-not-be-visible",
            gateway_key_id="kemo",
            request_id="must-not-be-visible",
            response_id="must-not-be-visible",
        )
        assert completed is not None
        await app.state.statistics.finish_invocation(
            completed,
            status="completed",
            usage=Usage(
                input_tokens=100,
                cached_input_tokens=40,
                output_tokens=20,
                total_tokens=120,
                measurement=UsageMeasurement(
                    mode="provider",
                    exact=True,
                    exact_fields=[
                        "input_tokens",
                        "cached_input_tokens",
                        "output_tokens",
                        "total_tokens",
                    ],
                ),
            ),
        )
        failed = await app.state.statistics.begin_invocation(
            task="llm",
            provider_id="fake",
            model="fake-model",
            tenant_id="must-not-be-visible",
            gateway_key_id="kemo",
            request_id="failed-request-must-not-be-visible",
        )
        assert failed is not None
        await app.state.statistics.finish_invocation(
            failed,
            status="failed",
            error_code="UPSTREAM_UNAVAILABLE",
            error_message="Authorization: Bearer status-secret api_key=upstream-secret-012345678901234567890123",
        )
        return completed.day

    return asyncio.run(scenario())


def test_status_api_is_read_only_dedicated_and_secret_free(
    tmp_path: Path, monkeypatch
) -> None:
    root = project(tmp_path)
    app = create_app(
        Settings(status_token="status-only-token"),
        live_config_root=root,
        statistics_root=root / "storage",
        )
    install_fixtures(app.state.registry, FakeProvider(), system_prompt="policy-v1")
    day = seed_statistics(app)
    monkeypatch.setattr(
        app.state.system_inspector,
        "cached_version_check",
        lambda: {
            "status": "up_to_date",
            "update_available": False,
            "local": {"version": "2.0.0", "protocol_version": "2.0"},
            "remote": {"version": "2.0.0", "protocol_version": "2.0"},
            "message": "当前已是最新版本",
        },
    )

    with TestClient(app) as client:
        assert client.get("/status").status_code == 401
        assert client.get(
            "/status", headers={"Authorization": "Bearer live-token"}
        ).status_code == 401
        assert client.post("/status", headers=STATUS_HEADERS).status_code == 405
        response = client.get(
            "/status",
            params={"date": day, "log_limit": 10},
            headers=STATUS_HEADERS,
        )
        assert client.get(
            "/model/capabilities",
            params={"model": "fake-model"},
            headers=STATUS_HEADERS,
        ).status_code == 401

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    payload = response.json()
    assert payload["object"] == "kemo.gateway_status"
    assert payload["protocol_version"] == "2.0"
    assert payload["registry"]["registered_provider_ids"] == ["gateway"]
    assert payload["registry"]["enabled_models"] == [
        {"model": "fake-model", "provider_id": "gateway"}
    ]
    assert payload["control"]["highest_priority_system_prompt"] == "policy-v1"
    assert payload["statistics"]["summary"]["calls"] == 2
    assert payload["statistics"]["token_cache_rate"] == 0.4
    assert payload["statistics"]["rankings"]["providers"][0]["id"] == "fake"
    assert payload["statistics"]["rankings"]["models"][0]["id"] == "fake-model"
    assert payload["statistics"]["rankings"]["gateway_keys"][0]["id"] == "kemo"
    assert payload["logs"]["last_invocation"] is not None
    assert payload["logs"]["successful"][0]["status"] == "completed"
    assert payload["logs"]["failed"][0]["error_code"] == "UPSTREAM_UNAVAILABLE"

    body = response.text
    for secret in (
        "status-only-token",
        "live-token",
        "secret",
        "must-not-be-visible",
        "failed-request-must-not-be-visible",
    ):
        assert secret not in body


def test_status_reports_routes_disabled_models_and_credential_cooldowns_without_connection_secrets(
    tmp_path, monkeypatch,
):
    def unavailable(request):
        return httpx.Response(503, json={"error": {"message": "api_key=private-upstream-error"}})

    with instance(tmp_path, monkeypatch, handler=unavailable,
                  settings=Settings(status_token="status-only-token")) as (client, app):
        add_upstream(client)
        register(client)
        add_upstream(client, "disabled")
        register(client, "disabled", model="disabled-model")
        assert mutate(client, "/upstreams/disabled", {"enabled": False}, method="PATCH").is_success
        assert mutate(client, "/models/disabled-model", {"enabled": False}, method="PATCH").is_success
        assert mutate(client, "/upstreams/local", {
            "base_url": "http://127.0.0.1:8881/private-upstream-url",
            "headers": {"X-Private": "private-header-value"},
            "profile": {"extra_body": {"private": "private-body-value"}},
            "note": "private-upstream-note",
        }, method="PATCH").is_success
        invoke(client, key(client)["token"], identity="req_status_failure")
        response = client.get("/status", headers=STATUS_HEADERS)
        assert response.status_code == 200, response.text
        payload = response.json()
        upstream = next(item for item in payload["upstreams"] if item["id"] == "local")
        credential = upstream["credentials"][0]
        assert credential["status"] == "unavailable" and credential["provider_status"] == 503
        assert credential["error_code"] == "PROVIDER_UNAVAILABLE" and credential["cooldown_seconds"] > 0
        assert payload["control"]["disabled_upstreams"] == ["disabled"]
        assert payload["control"]["disabled_models"] == ["disabled-model"]
        route = next(item for item in payload["routing"]["deployments"] if item["public_model"] == "stable")
        assert route["upstream_id"] == "local" and route["upstream_model"] == "remote"
        assert route["priority"] == 0 and route["weight"] == 1 and route["cooldown_seconds"] > 0
        assert payload["routing"]["max_attempts"] == app.state.registry.current.options.max_attempts
        assert any(not item["enabled"] for item in payload["routing"]["models"])
        for secret in (
            "fake-upstream-key-for-offline-tests", "private-upstream-url", "private-header-value",
            "private-body-value", "private-upstream-note", "private-upstream-error",
        ):
            assert secret not in response.text


def test_status_overview_uses_selected_period_cost_currency_and_bounded_rankings(tmp_path, monkeypatch):
    now = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("api.routes.status.usage_overview",
                        lambda store, period, zone, *, display_currency: usage_overview(
                            store, period, zone, now=now, display_currency=display_currency))
    with instance(tmp_path, monkeypatch, settings=Settings(
        status_token="status-only-token", statistics_timezone="Asia/Shanghai",
    )) as (client, app):
        add_upstream(client)
        register(client)
        register(client, model="other")
        current = state(client)
        for deployment in current["deployments"]:
            assert mutate(client, "/deployments/" + deployment["id"], {"price": {
                "input_per_million": 1, "output_per_million": 2, "currency": "USD",
            }}, method="PATCH").is_success
        assert mutate(client, "/settings", {"cost_display_currency": "CNY"}, method="PATCH").is_success
        token = key(client)["token"]
        for index, age in enumerate((
            timedelta(hours=1), timedelta(hours=20), timedelta(days=5),
            timedelta(days=20), timedelta(days=40),
        )):
            result = invoke(client, token, identity=f"req_status_period_{index}",
                            model="other" if index == 4 else "stable")
            assert result.status_code == 200, result.text
            with app.state.registry.store.transaction() as db:
                assert db.execute("UPDATE attempts SET started_at=? WHERE response_id=?",
                                  ((now - age).isoformat(), result.json()["id"])).rowcount == 1
        for period, calls in (("today", 1), ("24h", 2), ("7d", 3), ("30d", 4), ("all", 5)):
            response = client.get("/status", params={"period": period, "ranking_limit": 1},
                                  headers=STATUS_HEADERS)
            assert response.status_code == 200, response.text
            data = response.json()["statistics"]
            overview = data["overview"]
            assert overview["period"] == period and overview["timezone"] == "Asia/Shanghai"
            metrics = overview["summary"]
            assert metrics["calls"] == calls and metrics["success_rate"] == 1
            assert metrics["total_tokens"] == calls * 5
            assert metrics["costs"] == {"USD": pytest.approx(calls * .000007)}
            assert metrics["display_cost"]["currency"] == "CNY"
            assert metrics["display_cost"]["amount"] == pytest.approx(
                calls * .000007 * EXCHANGE_RATES["CNY"] / EXCHANGE_RATES["USD"])
            assert all(len(items) <= 1 for items in overview["rankings"].values())
            # The legacy fields still describe the selected calendar day.
            assert data["summary"]["calls"] == 5
            assert token not in response.text
        for params in ({"period": "90d"}, {"date": "invalid"}, {"ranking_limit": 0}):
            assert client.get("/status", params=params, headers=STATUS_HEADERS).status_code == 422


def test_status_resources_separate_running_installed_and_user_metadata(tmp_path, monkeypatch):
    official = tmp_path / "official"
    for relative in BUILTIN_PATHS.values():
        shutil.copytree(SOURCE_ROOT / relative, official / relative,
                        ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr("api.routes.status.SOURCE_ROOT", official)
    with instance(tmp_path, monkeypatch, settings=Settings(status_token="status-only-token")) as (client, app):
        template = extension_zip("provider_templates", identity="private-template", template={
            "adapter_id": "openai-chat", "base_url": "https://private-template-url.example",
            "headers": {"X-Private": "private-template-header"},
        })
        assert install_extension(client, "provider_templates", template).is_success
        adapter = extension_zip("protocol_adapters", identity="private-adapter",
                                code="raise RuntimeError('status must not execute this adapter')\n")
        assert install_extension(client, "protocol_adapters", adapter).is_success
        app.state.registry.resource_update_plan = {
            "available": True, "branch": "kemo-preview", "target_commit": "a" * 40,
            "resource_revision": "private-resource-revision",
            "groups": {"protocol_adapters": {
                "current": "1.0.0", "target": "9.0.0", "available": True,
                "changes": [{"id": "openai-chat", "current": "1.0.0", "target": "9.0.0"}],
            }},
        }
        manifest = official / BUILTIN_PATHS["protocol_adapters"] / "manifest.yaml"
        content = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        content["version"] = "9.0.0"
        manifest.write_text(yaml.safe_dump(content), encoding="utf-8")
        response = client.get("/status", headers=STATUS_HEADERS)
        assert response.status_code == 200, response.text
        resources = response.json()["resources"]
        assert resources["running"] == app.state.registry.resource_inventory
        assert resources["installed"]["protocol_adapters"]["version"] == "9.0.0"
        assert resources["restart_required"] is True and resources["error_code"] is None
        for kind in ("provider_templates", "protocol_adapters"):
            item = resources["user"][kind][0]
            assert item["source"] == "user" and item["version"] == "1.0.0"
            assert item["valid"] is True and item["enabled"] is False
        assert resources["update"]["branch"] == "kemo-preview"
        assert resources["update"]["groups"]["protocol_adapters"]["available"] is True
        for secret in ("private-template-url", "private-template-header", "private-resource-revision",
                       "status must not execute this adapter"):
            assert secret not in response.text
        manifest.write_text("api_key: private-invalid-resource\n", encoding="utf-8")
        response = client.get("/status", headers=STATUS_HEADERS)
        assert response.status_code == 200
        resources = response.json()["resources"]
        assert resources["installed"] is None and resources["restart_required"] is None
        assert resources["error_code"] == "RESOURCE_INVENTORY_UNAVAILABLE"
        assert "private-invalid-resource" not in response.text


def test_status_api_is_disabled_when_token_is_empty_or_reused(tmp_path: Path) -> None:
    root = project(tmp_path)
    disabled = create_app(
        Settings(status_token=""), live_config_root=root, )
    reused = create_app(
        Settings(status_token="live-token", api_keys={"live-token": PrincipalConfig("tenant", "agent")}),
        live_config_root=root,
        )

    with TestClient(disabled) as client:
        assert client.get("/status", headers=STATUS_HEADERS).status_code == 503
    with TestClient(reused) as client:
        response = client.get(
            "/status", headers={"Authorization": "Bearer live-token"}
        )
        assert response.status_code == 503
        assert "live-token" not in response.text


def test_status_log_redaction_recurses_through_nested_values() -> None:
    item = {
        "safe": "visible",
        "api_key": "short-api-secret",
        "gateway_key_id": "kemo",
        "nested": [
            {"accessToken": "short-access-secret", "safe": "kept"},
            {"authorization": "Bearer short-bearer-secret"},
        ],
    }

    safe = _safe_log_item(item)
    body = json.dumps(safe, ensure_ascii=False)

    assert safe["safe"] == "visible"
    assert safe["gateway_key_id"] == "kemo"
    assert safe["api_key"] == "[redacted]"
    assert safe["nested"][0]["accessToken"] == "[redacted]"
    assert safe["nested"][0]["safe"] == "kept"
    assert safe["nested"][1]["authorization"] == "[redacted]"
    for secret in ("short-api-secret", "short-access-secret", "short-bearer-secret"):
        assert secret not in body


def test_status_log_redaction_parses_double_and_single_quoted_json() -> None:
    messages = (
        '{"api_key": "double-quoted-secret", "nested": [{"signature": "sig-a"}]}',
        "{'api_key': 'single-quoted-secret', 'nested': [{'token': 'token-a'}]}",
    )

    for message in messages:
        safe = _safe_log_message(message)
        assert safe is not None
        assert "[redacted]" in safe
        for secret in (
            "double-quoted-secret",
            "single-quoted-secret",
            "sig-a",
            "token-a",
        ):
            assert secret not in safe


def test_status_log_redaction_hides_url_userinfo_and_query_secrets() -> None:
    message = (
        "https://user:shortpass@example.com/path?token=shorttoken"
        "&key=shortkey&signature=shortsignature&safe=1"
    )

    safe = _safe_log_message(message)

    assert safe is not None
    assert "https://[redacted]@example.com/path" in safe
    assert "token=[redacted]" in safe
    assert "key=[redacted]" in safe
    assert "signature=[redacted]" in safe
    assert "safe=1" in safe
    for secret in ("shortpass", "shorttoken", "shortkey", "shortsignature"):
        assert secret not in safe
