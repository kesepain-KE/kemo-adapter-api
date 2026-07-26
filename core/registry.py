"""Provider 包发现、注册和模型路由。"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from typing import Any

import providers

from core.live_config import LiveConfigSnapshot
from core.provider_contract import ProviderPackage


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderPackage] = {}
        self._models: dict[str, ProviderPackage] = {}
        self._bootstrap_settings: Mapping[str, Any] = {}
        self._live_revision = "empty"
        self._disabled_providers: frozenset[str] = frozenset()
        self._disabled_models: frozenset[str] = frozenset()
        self._applied_provider_settings: dict[str, dict[str, Any]] = {}

    def register(self, package: ProviderPackage) -> None:
        if not package.provider_id or package.provider_id.startswith("_"):
            raise ValueError("provider_id 必须是非下划线开头的稳定标识")
        if package.provider_id in self._providers:
            raise ValueError(f"Provider 重复注册: {package.provider_id}")
        for model in package.models:
            if model in self._models:
                raise ValueError(f"模型路由重复注册: {model}")
            if not model.startswith(f"{package.provider_id}/"):
                raise ValueError(f"模型必须以 {package.provider_id}/ 开头: {model}")
        self._providers[package.provider_id] = package
        for model in package.models:
            self._models[model] = package

    def resolve(self, model: str) -> ProviderPackage:
        provider_id = model.split("/", 1)[0]
        if provider_id in self._disabled_providers:
            raise LookupError(f"Provider 已禁用: {provider_id}")
        if model in self._disabled_models:
            raise LookupError(f"模型已禁用: {model}")
        return self.resolve_registered(model)

    def resolve_registered(self, model: str) -> ProviderPackage:
        """供已创建执行继续运行或取消，不应用新请求禁用策略。"""
        try:
            return self._models[model]
        except KeyError as exc:
            raise LookupError(f"没有注册模型: {model}") from exc

    @property
    def providers(self) -> Mapping[str, ProviderPackage]:
        return dict(self._providers)

    def discover(
        self,
        settings: Mapping[str, Any],
        live_settings: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """加载 ``providers/<id>/__init__.py:create_provider``。

        下划线开头目录（例如 ``_template``）不会加载。单个包的私有依赖不得泄漏到核心。
        """

        self._bootstrap_settings = settings
        live_settings = live_settings or {}
        prefix = f"{providers.__name__}."
        for module_info in pkgutil.iter_modules(providers.__path__, prefix):
            short_name = module_info.name.rsplit(".", 1)[-1]
            if short_name.startswith("_") or not module_info.ispkg:
                continue
            module = importlib.import_module(module_info.name)
            factory = getattr(module, "create_provider", None)
            if factory is None:
                raise RuntimeError(f"{module_info.name} 缺少 create_provider(settings)")
            package_settings = self._merge_settings(
                settings.get(short_name, {}), live_settings.get(short_name, {})
            )
            package = factory(package_settings)
            if not isinstance(package, ProviderPackage):
                raise TypeError(f"{module_info.name}.create_provider 返回值不符合 ProviderPackage")
            self.register(package)
            self._applied_provider_settings[short_name] = package_settings

    @staticmethod
    def _merge_settings(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = ProviderRegistry._merge_settings(merged[key], value)
            else:
                merged[key] = value
        return merged

    async def apply_live_config(self, snapshot: LiveConfigSnapshot) -> None:
        if snapshot.revision == self._live_revision:
            return
        for provider_id, package in self._providers.items():
            static = self._bootstrap_settings.get(provider_id, {})
            dynamic = snapshot.provider_settings.get(provider_id, {})
            merged = self._merge_settings(static, dynamic)
            if self._applied_provider_settings.get(provider_id) != merged:
                await package.reload_config(merged)
                self._applied_provider_settings[provider_id] = merged
        self._disabled_providers = snapshot.disabled_providers
        self._disabled_models = snapshot.disabled_models
        self._live_revision = snapshot.revision

    async def close(self) -> None:
        for package in self._providers.values():
            await package.close()
