"""Provider 包发现、注册和模型路由。"""

from __future__ import annotations

import asyncio
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
        # Provider code and manifests are loaded at process start.  If a
        # provider directory is removed while the gateway is running, keep the
        # already-created package only for executions that were admitted before
        # the removal.  It must not remain in the active registry: otherwise
        # new requests and the Web console can continue routing to a provider
        # that no longer exists on disk.
        self._retired_providers: dict[str, ProviderPackage] = {}
        self._retired_models: dict[str, ProviderPackage] = {}
        self._retired_package_ids: set[int] = set()
        self._retired_closing_ids: set[int] = set()
        self._package_references: dict[int, int] = {}
        # Only packages loaded by ``discover`` are tied to the on-disk
        # providers directory.  Tests and embedders may register in-memory
        # packages explicitly; those must not be retired just because their
        # live-config root has no matching directory.
        self._managed_provider_ids: set[str] = set()
        self._bootstrap_settings: Mapping[str, Any] = {}
        self._live_revision = "empty"
        self._disabled_providers: frozenset[str] = frozenset()
        self._disabled_models: frozenset[str] = frozenset()
        self._applied_provider_settings: dict[str, dict[str, Any]] = {}
        # Live reloads and shutdown can both await Provider code.  Serialize
        # those control-plane transitions so two concurrent requests cannot
        # reload the same package twice or retire it while shutdown is closing
        # it.  Retirement cleanup has its own lock because releases arrive
        # from producer tasks outside this control-plane lock.
        self._config_lock = asyncio.Lock()
        self._retirement_lock = asyncio.Lock()
        self._closed = False

    def register(self, package: ProviderPackage, *, managed: bool = False) -> None:
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
        if managed:
            self._managed_provider_ids.add(package.provider_id)
        for model in package.models:
            self._models[model] = package

    def resolve(self, model: str) -> ProviderPackage:
        try:
            package = self._models[model]
        except KeyError as exc:
            if model in self._retired_models:
                raise LookupError(f"Provider 已删除，模型不可用: {model}") from exc
            raise LookupError(f"没有注册模型: {model}") from exc
        if package.provider_id in self._disabled_providers:
            raise LookupError(f"Provider 已禁用: {package.provider_id}")
        if model in self._disabled_models:
            raise LookupError(f"模型已禁用: {model}")
        return package

    def resolve_registered(self, model: str) -> ProviderPackage:
        """供已创建执行继续运行或取消，不应用新请求禁用策略。"""
        package = self._models.get(model) or self._retired_models.get(model)
        if package is None:
            raise LookupError(f"没有注册模型: {model}")
        if id(package) in self._retired_closing_ids:
            raise LookupError(f"Provider 已删除，模型不可用: {model}")
        return package

    def acquire_registered(self, model: str) -> ProviderPackage:
        """Acquire a package reference for one admitted execution.

        A package can be retired between request validation and execution
        startup.  Holding this reference makes the hand-off atomic from the
        registry's point of view: retirement may remove the package from new
        routing, but it cannot close the instance while this execution still
        owns it.
        """

        package = self.resolve_registered(model)
        return self._retain_package(package)

    def acquire_active(self, model: str) -> ProviderPackage:
        """Acquire a reference for a new request after enablement checks."""

        package = self.resolve(model)
        return self._retain_package(package)

    def _retain_package(self, package: ProviderPackage) -> ProviderPackage:
        package_id = id(package)
        self._package_references[package_id] = (
            self._package_references.get(package_id, 0) + 1
        )
        return package

    async def release_registered(self, package: ProviderPackage) -> None:
        """Release an execution reference and reap an idle retired package."""

        package_id = id(package)
        references = self._package_references.get(package_id, 0)
        if references <= 0:
            # Ignore an untracked release.  This protects shutdown/error paths
            # that may defensively release an already-released handle and,
            # importantly, prevents an unretained stale package from being
            # closed accidentally.
            return
        if references == 1:
            self._package_references.pop(package_id, None)
        else:
            self._package_references[package_id] = references - 1
            return
        if package_id in self._retired_package_ids:
            await self._close_retired_package(package)

    async def _close_retired_package(self, package: ProviderPackage) -> None:
        async with self._retirement_lock:
            package_id = id(package)
            if package_id not in self._retired_package_ids:
                return
            if self._package_references.get(package_id, 0) > 0:
                return
            if package_id in self._retired_closing_ids:
                return
            self._retired_closing_ids.add(package_id)
            try:
                try:
                    await package.close()
                except Exception:
                    # Retirement cleanup must not turn a completed request
                    # into an error merely because a stale Provider client
                    # closes noisily.
                    logger.warning(
                        "Retired Provider close failed for %s (%s)",
                        getattr(package, "provider_id", "unknown"),
                        type(package).__name__,
                    )
            finally:
                self._retired_closing_ids.discard(package_id)
                self._retired_package_ids.discard(package_id)
                for provider_id, registered in list(self._retired_providers.items()):
                    if registered is package:
                        self._retired_providers.pop(provider_id, None)
                for model, registered in list(self._retired_models.items()):
                    if registered is package:
                        self._retired_models.pop(model, None)

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
            self.register(package, managed=True)
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
        async with self._config_lock:
            if self._closed:
                return
            await self._apply_live_config_unlocked(snapshot)

    async def _apply_live_config_unlocked(self, snapshot: LiveConfigSnapshot) -> None:
        if snapshot.revision == self._live_revision:
            return
        # ``LiveConfigManager`` fingerprints the provider directory set.  A
        # removed directory therefore reaches this method as a snapshot that
        # no longer contains that provider id.  Reconcile removals only after
        # all live configuration reloads succeed so a failed hot update keeps
        # the previous active registry intact.
        removed_provider_ids = [
            provider_id
            for provider_id in self._providers
            if provider_id in self._managed_provider_ids
            and provider_id not in snapshot.provider_settings
        ]
        pending: list[tuple[str, ProviderPackage, dict[str, Any], dict[str, Any]]] = []
        for provider_id, package in self._providers.items():
            if provider_id in removed_provider_ids:
                # There is no candidate configuration to reload for a
                # directory that disappeared.  Retire it below instead of
                # attempting to resurrect its old static settings.
                continue
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
        for provider_id in removed_provider_ids:
            package = self._providers.pop(provider_id, None)
            if package is None:
                continue
            self._retired_providers[provider_id] = package
            self._retired_package_ids.add(id(package))
            self._applied_provider_settings.pop(provider_id, None)
            for model in package.models:
                if self._models.get(model) is package:
                    self._models.pop(model, None)
                    self._retired_models[model] = package
            await self._close_retired_package(package)
        self._disabled_providers = snapshot.disabled_providers
        self._disabled_models = snapshot.disabled_models
        self._live_revision = snapshot.revision

    async def close(self) -> None:
        async with self._config_lock:
            if self._closed:
                return
            self._closed = True
            async with self._retirement_lock:
                packages = list(self._providers.values()) + list(
                    self._retired_providers.values()
                )
                seen: set[int] = set()
                for package in packages:
                    if id(package) in seen:
                        continue
                    seen.add(id(package))
                    try:
                        await package.close()
                    except Exception:
                        logger.warning(
                            "Provider close failed for %s (%s)",
                            getattr(package, "provider_id", "unknown"),
                            type(package).__name__,
                        )
                self._providers.clear()
                self._models.clear()
                self._retired_providers.clear()
                self._retired_models.clear()
                self._retired_package_ids.clear()
                self._retired_closing_ids.clear()
                self._package_references.clear()
