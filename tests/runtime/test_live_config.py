from __future__ import annotations

from tests.support.project import project, write_json

import asyncio
import json
from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from core.executor import GatewayExecutor
from core.live_config import LiveConfigManager, LiveConfigSnapshot
from core.registry import ProviderRegistry
from core.stores import InMemoryExecutionStore
from tests.support.llm import FakeProvider


class ReloadableFakeProvider(FakeProvider):
    def __init__(self) -> None:
        self.applied_settings: list[dict] = []
        self.closed = False
        self.close_calls = 0

    async def reload_config(self, settings) -> None:
        self.applied_settings.append(dict(settings))

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class FirstReloadableProvider(ReloadableFakeProvider):
    provider_id = "first"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"first-model"})


class BlockingReloadProvider(FirstReloadableProvider):
    def __init__(self) -> None:
        super().__init__()
        self.reload_started = asyncio.Event()
        self.reload_release = asyncio.Event()

    async def reload_config(self, settings) -> None:
        self.reload_started.set()
        await self.reload_release.wait()
        await super().reload_config(settings)


class FailingCloseProvider(FirstReloadableProvider):
    async def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("close failed")


class FailingReloadProvider(ReloadableFakeProvider):
    provider_id = "second"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"second-model"})

    async def reload_config(self, settings) -> None:
        if settings.get("mode") == "bad":
            raise ValueError("candidate rejected")
        await super().reload_config(settings)


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

        # A rollback/repaired write restores the known-good fingerprint.  The
        # previous parse error must not remain visible forever.
        write_json(
            root / "core" / "live_control.json",
            {
                "highest_priority_system_prompt": "policy-v1",
                "disabled_providers": [],
                "disabled_models": [],
            },
        )
        restored = await manager.refresh()
        # Repairing the file causes a fresh valid snapshot; identity is not
        # guaranteed even though the effective configuration matches.
        assert restored.gateway_system_prompt == valid.gateway_system_prompt
        assert restored.disabled_providers == valid.disabled_providers
        assert restored.disabled_models == valid.disabled_models
        assert manager.last_error is None

    asyncio.run(scenario())


def test_registry_rolls_back_already_reloaded_provider_when_later_provider_fails() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        first = FirstReloadableProvider()
        second = FailingReloadProvider()
        registry.register(first)
        registry.register(second)

        old = LiveConfigSnapshot(
            revision="old",
            provider_settings={
                "first": {"mode": "old"},
                "second": {"mode": "old"},
            },
        )
        await registry.apply_live_config(old)
        assert first.applied_settings[-1]["mode"] == "old"
        assert second.applied_settings[-1]["mode"] == "old"

        new = LiveConfigSnapshot(
            revision="new",
            provider_settings={
                "first": {"mode": "new"},
                "second": {"mode": "bad"},
            },
        )
        with pytest.raises(ValueError, match="candidate rejected"):
            await registry.apply_live_config(new)

        assert first.applied_settings[-1]["mode"] == "old"
        assert registry._live_revision == "old"
        assert registry._applied_provider_settings["first"]["mode"] == "old"

    asyncio.run(scenario())


def test_registry_retires_provider_removed_from_live_provider_set() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        provider = FirstReloadableProvider()
        registry.register(provider, managed=True)
        package = registry.acquire_registered("first-model")

        loaded = LiveConfigSnapshot(
            revision="loaded",
            provider_settings={"first": {"mode": "active"}},
        )
        await registry.apply_live_config(loaded)
        assert "first" in registry.providers
        assert registry.resolve_registered("first-model") is provider

        removed = LiveConfigSnapshot(
            revision="removed",
            provider_settings={},
        )
        await registry.apply_live_config(removed)

        # The provider disappears from all active/catalog views and cannot
        # receive new work, while an already-created execution can still
        # resolve its package for completion/cancellation.
        assert "first" not in registry.providers
        assert registry.resolve_registered("first-model") is provider
        assert provider.closed is False

        await registry.release_registered(package)
        assert provider.closed is True
        assert provider.close_calls == 1
        # A defensive duplicate release must not close the retired package a
        # second time or mutate another package's reference count.
        await registry.release_registered(package)
        assert provider.close_calls == 1
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve_registered("first-model")

        await registry.close()

    asyncio.run(scenario())


def test_registry_retires_provider_when_its_real_directory_is_removed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = project(tmp_path)
        fake_directory = root / "providers" / "fake"
        first_directory = root / "providers" / "first"
        fake_directory.rename(first_directory)
        (first_directory / "config.json").unlink()
        (first_directory / "secrets.json").unlink()

        manager = LiveConfigManager(root)
        initial = await manager.refresh()
        assert "first" in initial.provider_settings

        registry = ProviderRegistry()
        provider = FirstReloadableProvider()
        registry.register(provider, managed=True)
        await registry.apply_live_config(initial)
        held = registry.acquire_registered("first-model")

        shutil.rmtree(first_directory)
        removed = await manager.refresh()
        assert "first" not in removed.provider_settings
        await registry.apply_live_config(removed)

        assert "first" not in registry.providers
        with pytest.raises(LookupError, match="Provider 已删除"):
            registry.resolve("first-model")
        assert registry.resolve_registered("first-model") is provider
        assert provider.closed is False

        await registry.release_registered(held)
        assert provider.closed is True
        assert provider.close_calls == 1

    asyncio.run(scenario())


def test_registry_closes_retired_provider_without_references() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        provider = FirstReloadableProvider()
        registry.register(provider, managed=True)
        await registry.apply_live_config(
            LiveConfigSnapshot(revision="loaded", provider_settings={"first": {}})
        )

        await registry.apply_live_config(
            LiveConfigSnapshot(revision="removed", provider_settings={})
        )

        assert provider.closed is True
        assert provider.close_calls == 1
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve_registered("first-model")

    asyncio.run(scenario())


def test_registry_serializes_concurrent_live_config_apply() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        provider = BlockingReloadProvider()
        registry.register(provider)
        snapshot = LiveConfigSnapshot(
            revision="concurrent", provider_settings={"first": {"mode": "new"}}
        )

        first = asyncio.create_task(registry.apply_live_config(snapshot))
        await provider.reload_started.wait()
        second = asyncio.create_task(registry.apply_live_config(snapshot))
        await asyncio.sleep(0)
        assert second.done() is False

        provider.reload_release.set()
        await asyncio.gather(first, second)
        assert provider.applied_settings == [{"mode": "new"}]

    asyncio.run(scenario())


def test_registry_ignores_retired_provider_close_failure() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        provider = FailingCloseProvider()
        registry.register(provider, managed=True)
        await registry.apply_live_config(
            LiveConfigSnapshot(revision="loaded", provider_settings={"first": {}})
        )
        await registry.apply_live_config(
            LiveConfigSnapshot(revision="removed", provider_settings={})
        )

        assert provider.close_calls == 1
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve_registered("first-model")

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


def test_live_key_model_whitelist_distinguishes_allow_all_and_deny_all(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root = project(tmp_path)
        manager = LiveConfigManager(root)
        missing = await manager.refresh()
        assert missing.api_keys["live-token"].allowed_models is None

        payload = json.loads((root / "api" / "keys.json").read_text(encoding="utf-8"))
        payload["keys"]["live-token"]["allowed_models"] = []
        write_json(root / "api" / "keys.json", payload)
        deny_all = await manager.refresh()
        assert deny_all.api_keys["live-token"].allowed_models == frozenset()

        payload["keys"]["live-token"]["allowed_models"] = ["fake-model"]
        write_json(root / "api" / "keys.json", payload)
        allow_list = await manager.refresh()
        assert allow_list.api_keys["live-token"].allowed_models == frozenset(
            {"fake-model"}
        )

    asyncio.run(scenario())
