from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from core.models import Usage, UsageMeasurement
from tests.test_live_config import project
from tests.test_provider_boundary import FakeProvider


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
        discover_providers=False,
    )
    app.state.registry.register(FakeProvider())
    day = seed_statistics(app)
    monkeypatch.setattr(
        app.state.system_inspector,
        "cached_version_check",
        lambda: {
            "status": "up_to_date",
            "update_available": False,
            "local": {"version": "0.5.0", "protocol_version": "1.0"},
            "remote": {"version": "0.5.0", "protocol_version": "1.0"},
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
    assert payload["registry"]["registered_provider_ids"] == ["fake"]
    assert payload["registry"]["enabled_models"] == [
        {"model": "fake-model", "provider_id": "fake"}
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


def test_status_api_is_disabled_when_token_is_empty_or_reused(tmp_path: Path) -> None:
    root = project(tmp_path)
    disabled = create_app(
        Settings(status_token=""), live_config_root=root, discover_providers=False
    )
    reused = create_app(
        Settings(status_token="live-token"),
        live_config_root=root,
        discover_providers=False,
    )

    with TestClient(disabled) as client:
        assert client.get("/status", headers=STATUS_HEADERS).status_code == 503
    with TestClient(reused) as client:
        response = client.get(
            "/status", headers={"Authorization": "Bearer live-token"}
        )
        assert response.status_code == 503
        assert "live-token" not in response.text
