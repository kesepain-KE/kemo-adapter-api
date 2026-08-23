"""Provider 包发现、注册和模型路由。"""

from __future__ import annotations

import copy
import importlib
import logging
import pkgutil
from collections.abc import Mapping
from typing import Any

import providers

from core.live_config import LiveConfigSnapshot
from core.provider_keys import RoutedProviderPackage, normalize_provider_keys
from core.provider_contract import ProviderPackage


logger = logging.getLogger(__name__)


def _is_missing_provider_key_error(exc: ValueError) -> bool:
    """Return whether a factory error specifically means no API key was supplied.

    The multi-key compatibility fallback must not hide unrelated Provider
    configuration errors (for example an invalid endpoint or model list).
    """

    message = str(exc).lower().replace(" ", "")
    return any(
        marker in message
        for marker in (
            "缺少api_key",
            "缺少apikey",
            "需要api_key",
            "需要apikey",
            "missingapi_key",
            "missingapikey",
            "api_keyrequired",
            "apikeyrequired",
            "api_keyisrequired",
            "apikeyisrequired",
        )
    )


def _factory_settings(settings: Mapping[str, Any], pool: list[tuple[str, str, bool]]) -> dict[str, Any]:
    """Provide legacy Provider code with the first canonical pool key in memory."""

    normalized = dict(settings)
    first = next((entry for entry in pool if entry[2]), None)
    if first is not None:
        normalized["api_key"] = first[1]
        normalized["api_key_id"] = first[0]
    else:
        # An explicit pool with every entry disabled must never fall back to a
        # stale legacy api_key left in config/secrets.  Leave the Provider
        # factory without credentials so it fails closed instead of silently
        # routing through a disabled key.
        normalized.pop("api_key", None)
        normalized.pop("api_key_id", None)
    return normalized


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
            prefix = f"{package.provider_id}-"
            if not model.startswith(prefix) or len(model) == len(prefix):
                raise ValueError(f"模型必须以 {prefix} 开头且包含模型名: {model}")
        self._providers[package.provider_id] = package
        for model in package.models:
            self._models[model] = package

    def resolve(self, model: str) -> ProviderPackage:
        package = self.resolve_registered(model)
        if package.provider_id in self._disabled_providers:
            raise LookupError(f"Provider 已禁用: {package.provider_id}")
        if model in self._disabled_models:
            raise LookupError(f"模型已禁用: {model}")
        return package

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
            pool = normalize_provider_keys(package_settings)
            if "api_keys" in package_settings and not pool:
                raise ValueError(
                    f"Provider {short_name} 的 api_keys 不能为空；请至少保留一把启用密钥"
                )
            if pool and not any(enabled for _, _, enabled in pool):
                raise ValueError(
                    f"Provider {short_name} 没有启用的 api_keys；请启用至少一把密钥，"
                    "或使用 Provider 启停策略"
                )
            factory_settings = _factory_settings(package_settings, pool)
            first_enabled = next((entry for entry in pool if entry[2]), None)

            def build_key_package(
                secret: str,
                routed_settings: Mapping[str, Any],
                provider_factory=factory,
            ) -> ProviderPackage:
                return provider_factory(
                    {**dict(routed_settings), "api_key": secret, "api_keys": None}
                )

            try:
                package = factory(factory_settings)
            except ValueError as exc:
                if len(pool) <= 1 or not _is_missing_provider_key_error(exc):
                    raise
                first = next((entry for entry in pool if entry[2]), None)
                if first is None:
                    raise
                package = build_key_package(first[1], package_settings)
            # 即使只有一个 legacy api_key，也要包装成可观测的密钥池，
            # 否则 Provider 能调用但 Web 控制台会误显示“尚未配置密钥”。
            if pool and not hasattr(package, "key_statuses"):
                packages: list[tuple[str, ProviderPackage]] = []
                for key_id, secret, enabled in pool:
                    if not enabled:
                        continue
                    # The initial factory call already built this package with
                    # the first enabled key. Reuse it instead of opening a
                    # second HTTP client for the same key.
                    key_package = (
                        package
                        if first_enabled is not None and key_id == first_enabled[0]
                        else build_key_package(secret, package_settings)
                    )
                    packages.append((key_id, key_package))
                if packages:
                    package = RoutedProviderPackage(
                        short_name,
                        packages,
                        key_secrets={key_id: secret for key_id, secret, _ in pool},
                        configured_keys=[(key_id, enabled) for key_id, _, enabled in pool],
                        package_factory=build_key_package,
                    )
            if not isinstance(package, ProviderPackage):
                raise TypeError(f"{module_info.name}.create_provider 返回值不符合 ProviderPackage")
            if package.provider_id != short_name:
                raise ValueError(
                    f"Provider ID 必须与目录名一致: 目录={short_name}, "
                    f"provider_id={package.provider_id}"
                )
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
        pending: list[tuple[str, ProviderPackage, dict[str, Any], dict[str, Any]]] = []
        for provider_id, package in self._providers.items():
            static = self._bootstrap_settings.get(provider_id, {})
            dynamic = snapshot.provider_settings.get(provider_id, {})
            merged = self._merge_settings(static, dynamic)
            previous = self._applied_provider_settings.get(provider_id, {})
            if previous != merged:
                pending.append(
                    (
                        provider_id,
                        package,
                        copy.deepcopy(previous),
                        copy.deepcopy(merged),
                    )
                )

        attempted: list[tuple[str, ProviderPackage, dict[str, Any]]] = []
        try:
            for provider_id, package, previous, merged in pending:
                # Include the current package before awaiting it.  A Provider
                # contract promises an atomic reload, but attempting the old
                # settings on failure also repairs implementations that
                # changed an internal field before discovering a bad option.
                attempted.append((provider_id, package, previous))
                await package.reload_config(merged)
        except Exception:
            for provider_id, package, previous in reversed(attempted):
                try:
                    await package.reload_config(previous)
                except Exception as rollback_error:
                    # Keep the original error for the caller.  The type name
                    # is enough for operators and cannot leak vendor details.
                    logger.error(
                        "Provider live config rollback failed for %s (%s)",
                        provider_id,
                        type(rollback_error).__name__,
                    )
            raise

        for provider_id, _, _, merged in pending:
            self._applied_provider_settings[provider_id] = merged
        self._disabled_providers = snapshot.disabled_providers
        self._disabled_models = snapshot.disabled_models
        self._live_revision = snapshot.revision

    async def close(self) -> None:
        for package in self._providers.values():
            await package.close()
