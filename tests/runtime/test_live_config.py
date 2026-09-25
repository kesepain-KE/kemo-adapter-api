from __future__ import annotations

from tests.support.project import project, write_json

import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import Settings
from core.executor import GatewayExecutor
from core.live_config import LiveConfigManager, LiveConfigSnapshot
from core.models import ModelCapabilities
from core.provider_contract import ProviderPackage
from core.registry import ProviderRegistry
from core.stores import InMemoryExecutionStore
from tests.support.llm import FakeProvider, request


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


class DeclarativeCatalogProvider(ProviderPackage):
    provider_id = "catalog"
    instances: list["DeclarativeCatalogProvider"] = []
    validation_started: asyncio.Event | None = None
    validation_release: asyncio.Event | None = None

    def __init__(self, settings) -> None:
        self._models = frozenset(settings.get("catalog_models", ["catalog-one"]))
        self._streaming = bool(settings.get("catalog_streaming", False))
        self._invalid_capability = bool(settings.get("invalid_capability", False))
        self._block_validation = bool(settings.get("block_validation", False))
        self.closed = False
        self.close_calls = 0
        self.instances.append(self)

    @property
    def models(self) -> frozenset[str]:
        return self._models

    async def capabilities(self, model: str) -> ModelCapabilities:
        if model not in self._models:
            raise LookupError(model)
        if self._block_validation and self.validation_started is not None:
            self.validation_started.set()
            if self.validation_release is not None:
                await self.validation_release.wait()
        return ModelCapabilities(
            model="catalog-wrong" if self._invalid_capability else model,
            input_modalities=["text"],
            output_modalities=["text"],
            streaming=self._streaming,
        )

    def requires_catalog_rebuild(self, previous_settings, new_settings) -> bool:
        fields = (
            "catalog_models",
            "catalog_streaming",
            "invalid_capability",
            "block_validation",
            "factory_failure",
        )
        return any(previous_settings.get(key) != new_settings.get(key) for key in fields)

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1


def discover_catalog_provider(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict,
) -> tuple[ProviderRegistry, DeclarativeCatalogProvider]:
    DeclarativeCatalogProvider.instances = []
    DeclarativeCatalogProvider.validation_started = None
    DeclarativeCatalogProvider.validation_release = None
    module_info = SimpleNamespace(name="providers.catalog", ispkg=True)
    monkeypatch.setattr("core.registry.pkgutil.iter_modules", lambda *_: [module_info])

    def create_provider(values):
        if values.get("factory_failure"):
            raise RuntimeError(f"private={values['factory_failure']}")
        return DeclarativeCatalogProvider(values)

    monkeypatch.setattr(
        "core.registry.importlib.import_module",
        lambda *_: SimpleNamespace(create_provider=create_provider),
    )
    registry = ProviderRegistry()
    registry.discover({"catalog": settings})
    package = registry.providers["catalog"]
    assert isinstance(package, DeclarativeCatalogProvider)
    return registry, package


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


def test_registry_atomically_rebuilds_opted_in_catalog_without_interrupting_old_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, original = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one"], "catalog_streaming": False},
        )
        gateway = GatewayExecutor(registry, InMemoryExecutionStore())
        context = gateway.make_context(
            tenant_id="tenant-catalog",
            subject_id="subject-catalog",
            request_id="req-catalog",
        )
        record, created, held = await gateway.prepare(
            request(stream=False).model_copy(update={"model": "catalog-one"}),
            context,
        )
        assert created is True
        assert held is original

        await registry.apply_live_config(
            LiveConfigSnapshot(
                revision="catalog-v2",
                provider_settings={
                    "catalog": {
                        "catalog_models": ["catalog-one", "catalog-two"],
                        "catalog_streaming": True,
                    }
                },
            )
        )

        replacement = registry.resolve("catalog-two")
        assert replacement is registry.resolve("catalog-one")
        assert replacement is not original
        assert (await replacement.capabilities("catalog-two")).streaming is True
        assert original.closed is False

        cancellation_package = registry.acquire_registered(
            "catalog-one", response_id=record.response_id
        )
        assert cancellation_package is original
        await registry.release_registered(cancellation_package)

        registry.unbind_execution(record.response_id, held)
        await registry.release_registered(held)
        assert original.closed is True
        assert original.close_calls == 1
        await registry.close()

    asyncio.run(scenario())


def test_registry_does_not_publish_candidate_before_catalog_validation_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, original = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one"]},
        )
        DeclarativeCatalogProvider.validation_started = asyncio.Event()
        DeclarativeCatalogProvider.validation_release = asyncio.Event()
        apply_task = asyncio.create_task(
            registry.apply_live_config(
                LiveConfigSnapshot(
                    revision="catalog-blocked",
                    provider_settings={
                        "catalog": {
                            "catalog_models": ["catalog-one", "catalog-two"],
                            "block_validation": True,
                        }
                    },
                )
            )
        )

        await DeclarativeCatalogProvider.validation_started.wait()
        assert registry.resolve("catalog-one") is original
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve("catalog-two")

        DeclarativeCatalogProvider.validation_release.set()
        await apply_task
        assert registry.resolve("catalog-one") is not original
        assert registry.resolve("catalog-two") is registry.resolve("catalog-one")
        await registry.close()

    asyncio.run(scenario())


def test_registry_catalog_rebuild_can_remove_model_after_old_generation_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, original = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one", "catalog-two"]},
        )
        held = registry.acquire_active("catalog-one")

        await registry.apply_live_config(
            LiveConfigSnapshot(
                revision="catalog-remove",
                provider_settings={
                    "catalog": {"catalog_models": ["catalog-two"]}
                },
            )
        )

        with pytest.raises(LookupError, match="Provider 已删除"):
            registry.resolve("catalog-one")
        assert registry.resolve_registered("catalog-one") is original
        assert registry.resolve("catalog-two") is not original

        await registry.release_registered(held)
        assert original.closed is True
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve_registered("catalog-one")
        await registry.close()

    asyncio.run(scenario())


def test_registry_rejects_invalid_catalog_candidate_and_keeps_old_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, original = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one"]},
        )

        with pytest.raises(ValueError, match="能力声明模型名不一致"):
            await registry.apply_live_config(
                LiveConfigSnapshot(
                    revision="catalog-invalid",
                    provider_settings={
                        "catalog": {
                            "catalog_models": ["catalog-one", "catalog-two"],
                            "invalid_capability": True,
                        }
                    },
                )
            )

        assert registry.resolve("catalog-one") is original
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve("catalog-two")
        assert registry._live_revision == "empty"
        candidate = DeclarativeCatalogProvider.instances[-1]
        assert candidate is not original
        assert candidate.closed is True
        await registry.close()

    asyncio.run(scenario())


def test_registry_catalog_candidate_error_does_not_expose_private_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, original = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one"]},
        )
        secret = "upstream-secret-must-not-appear"

        with pytest.raises(ValueError) as captured:
            await registry.apply_live_config(
                LiveConfigSnapshot(
                    revision="catalog-factory-failure",
                    provider_settings={
                        "catalog": {
                            "catalog_models": ["catalog-one", "catalog-two"],
                            "factory_failure": secret,
                        }
                    },
                )
            )

        assert secret not in str(captured.value)
        assert "RuntimeError" in str(captured.value)
        assert registry.resolve("catalog-one") is original
        await registry.close()

    asyncio.run(scenario())


def test_registry_discards_catalog_candidate_when_another_provider_reload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, original = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one"]},
        )
        failing = FailingReloadProvider()
        registry.register(failing)
        await registry.apply_live_config(
            LiveConfigSnapshot(
                revision="transaction-old",
                provider_settings={
                    "catalog": {"catalog_models": ["catalog-one"]},
                    "second": {"mode": "old"},
                },
            )
        )

        with pytest.raises(ValueError, match="candidate rejected"):
            await registry.apply_live_config(
                LiveConfigSnapshot(
                    revision="transaction-new",
                    provider_settings={
                        "catalog": {
                            "catalog_models": ["catalog-one", "catalog-two"]
                        },
                        "second": {"mode": "bad"},
                    },
                )
            )

        assert registry.resolve("catalog-one") is original
        with pytest.raises(LookupError, match="没有注册模型"):
            registry.resolve("catalog-two")
        assert registry._live_revision == "transaction-old"
        candidate = DeclarativeCatalogProvider.instances[-1]
        assert candidate is not original
        assert candidate.closed is True
        assert failing.applied_settings[-1] == {"mode": "old"}
        await registry.close()

    asyncio.run(scenario())


def test_registry_keeps_multiple_retired_catalog_generations_until_each_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        registry, first = discover_catalog_provider(
            monkeypatch,
            {"catalog_models": ["catalog-one"]},
        )
        first_held = registry.acquire_active("catalog-one")

        await registry.apply_live_config(
            LiveConfigSnapshot(
                revision="catalog-second",
                provider_settings={
                    "catalog": {
                        "catalog_models": ["catalog-one"],
                        "catalog_streaming": True,
                    }
                },
            )
        )
        second = registry.resolve("catalog-one")
        second_held = registry.acquire_active("catalog-one")

        await registry.apply_live_config(
            LiveConfigSnapshot(
                revision="catalog-third",
                provider_settings={
                    "catalog": {
                        "catalog_models": ["catalog-one", "catalog-two"],
                        "catalog_streaming": True,
                    }
                },
            )
        )
        third = registry.resolve("catalog-one")
        assert first is not second and second is not third
        assert first.closed is False
        assert second.closed is False

        await registry.release_registered(first_held)
        assert first.closed is True
        assert second.closed is False
        await registry.release_registered(second_held)
        assert second.closed is True
        await registry.close()

    asyncio.run(scenario())


def test_executor_releases_provider_generation_binding_after_terminal_response() -> None:
    async def scenario() -> None:
        for streaming in (False, True):
            registry = ProviderRegistry()
            registry.register(FakeProvider())
            gateway = GatewayExecutor(registry, InMemoryExecutionStore())
            context = gateway.make_context(
                tenant_id="tenant-binding",
                subject_id="subject-binding",
                request_id=f"req-binding-{streaming}",
            )
            payload = request(stream=streaming).model_copy(
                update={"request_id": f"req-binding-{streaming}"}
            )
            if streaming:
                _ = [event async for event in gateway.stream(payload, context)]
            else:
                await gateway.execute(payload, context)
            assert registry._execution_packages == {}
            await registry.close()

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
