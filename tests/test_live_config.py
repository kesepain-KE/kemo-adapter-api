from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from core.executor import GatewayExecutor
from core.live_config import LiveConfigManager
from core.registry import ProviderRegistry
from core.stores import InMemoryExecutionStore
from tests.test_provider_boundary import FakeProvider


class ReloadableFakeProvider(FakeProvider):
    def __init__(self) -> None:
        self.applied_settings: list[dict] = []

    async def reload_config(self, settings) -> None:
        self.applied_settings.append(dict(settings))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def project(tmp_path: Path) -> Path:
    write_json(tmp_path / "api" / "runtime.json", {"gateway_api": {"enabled": True}})
    write_json(
        tmp_path / "api" / "keys.json",
        {
            "keys": {
                "live-token": {
                    "tenant_id": "tenant-live",
                    "subject_id": "agent-live",
                    "scopes": ["model:invoke"],
                }
            }
        },
    )
    write_json(
        tmp_path / "core" / "live_control.json",
        {
            "highest_priority_system_prompt": "policy-v1",
            "disabled_providers": [],
            "disabled_models": [],
        },
    )
    write_json(tmp_path / "providers" / "fake" / "config.json", {"base_url": "v1"})
    write_json(tmp_path / "providers" / "fake" / "secrets.json", {"api_key": "secret"})
    return tmp_path


def test_live_config_refreshes_only_supported_runtime_controls(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = project(tmp_path)
        manager = LiveConfigManager(root)
        first = await manager.refresh()
        assert first.gateway_system_prompt == "policy-v1"
        assert first.api_keys["live-token"].tenant_id == "tenant-live"
        assert first.provider_settings["fake"] == {
            "base_url": "v1",
            "api_key": "secret",
        }

        write_json(
            root / "core" / "live_control.json",
            {
                "highest_priority_system_prompt": "policy-v2-longer",
                "disabled_providers": [],
                "disabled_models": ["fake-model"],
            },
        )
        second = await manager.refresh()
        assert second.revision != first.revision
        assert second.gateway_system_prompt == "policy-v2-longer"
        assert second.disabled_models == frozenset({"fake-model"})

        registry = ProviderRegistry()
        provider = ReloadableFakeProvider()
        registry.register(provider)
        await registry.apply_live_config(second)
        assert provider.applied_settings[-1] == {"base_url": "v1", "api_key": "secret"}
        with pytest.raises(LookupError, match="模型已禁用"):
            registry.resolve("fake-model")
        assert registry.resolve_registered("fake-model") is provider

        gateway = GatewayExecutor(registry, InMemoryExecutionStore(), manager)
        context = gateway.make_context(
            tenant_id="tenant-live", subject_id="agent-live", request_id="req-live"
        )
        assert context.gateway_system_prompt == "policy-v2-longer"
        assert context.live_config_revision == second.revision

        write_json(root / "providers" / "fake" / "config.json", {"base_url": "v2-longer"})
        third = await manager.refresh()
        await registry.apply_live_config(third)
        assert provider.applied_settings[-1] == {
            "base_url": "v2-longer",
            "api_key": "secret",
        }

    asyncio.run(scenario())


def test_invalid_hot_config_keeps_last_known_good_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = project(tmp_path)
        manager = LiveConfigManager(root)
        valid = await manager.refresh()
        (root / "core" / "live_control.json").write_text("{invalid", encoding="utf-8")

        rejected = await manager.refresh()
        assert rejected is valid
        assert manager.last_error == "JSONDecodeError: live config reload rejected"

    asyncio.run(scenario())


def test_gateway_api_key_file_is_hot_loaded_without_environment_restart(tmp_path: Path) -> None:
    root = project(tmp_path)
    app = create_app(Settings(), live_config_root=root, discover_providers=False)
    with TestClient(app) as client:
        first = client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
            headers={"Authorization": "Bearer live-token"},
        )
        assert first.status_code == 404

        write_json(
            root / "api" / "keys.json",
            {
                "keys": {
                    "replacement-token-longer": {
                        "tenant_id": "tenant-live",
                        "subject_id": "agent-live",
                        "scopes": ["model:invoke"],
                    }
                }
            },
        )
        old_key = client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
            headers={"Authorization": "Bearer live-token"},
        )
        new_key = client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
            headers={"Authorization": "Bearer replacement-token-longer"},
        )
        assert old_key.status_code == 401
        assert new_key.status_code == 404

        write_json(root / "api" / "runtime.json", {"gateway_api": {"enabled": False}})
        disabled = client.get(
            "/model/capabilities",
            params={"model": "unknown-model"},
            headers={"Authorization": "Bearer replacement-token-longer"},
        )
        assert disabled.status_code == 503
        # 关闭新 API 请求不影响已有 Response 的查询和取消入口。
        query_existing = client.get(
            "/model/responses/resp_missing",
            headers={"Authorization": "Bearer replacement-token-longer"},
        )
        cancel_existing = client.post(
            "/model/responses/resp_missing/cancel",
            headers={"Authorization": "Bearer replacement-token-longer"},
        )
        assert query_existing.status_code == 404
        assert cancel_existing.status_code == 404
