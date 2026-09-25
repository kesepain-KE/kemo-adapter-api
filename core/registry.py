"""Provider 包发现、注册和模型路由。"""

from __future__ import annotations

import asyncio
import copy
import importlib
import logging
import pkgutil
from collections.abc import Mapping
from typing import Any, Callable

import providers

from core.live_config import LiveConfigSnapshot
from core.models import ModelCapabilities
from core.provider_keys import RoutedProviderPackage, normalize_provider_keys
from core.provider_contract import ProviderPackage


logger = logging.getLogger(__name__)


ProviderFactory = Callable[[Mapping[str, Any]], ProviderPackage]


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
        # Key retired generations by object identity rather than provider ID.
        # A Provider may be replaced more than once while older generations
        # still serve in-flight requests; a provider-id keyed dictionary would
        # lose those older instances during shutdown.
        self._retired_providers: dict[int, ProviderPackage] = {}
        self._retired_models: dict[str, ProviderPackage] = {}
        self._retired_package_ids: set[int] = set()
        self._retired_closing_ids: set[int] = set()
        self._package_references: dict[int, int] = {}
        # Bind active gateway response IDs to the exact Provider generation
        # that created them.  A catalog swap may publish a new package for the
        # same model while an older request is still cancellable; model-only
        # lookup would otherwise send cancel to the wrong upstream client.
        self._execution_packages: dict[str, ProviderPackage] = {}
        # Only packages loaded by ``discover`` are tied to the on-disk
        # providers directory.  Tests and embedders may register in-memory
        # packages explicitly; those must not be retired just because their
        # live-config root has no matching directory.
        self._managed_provider_ids: set[str] = set()
        self._provider_factories: dict[str, ProviderFactory] = {}
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

    def resolve_registered(
        self, model: str, *, response_id: str | None = None
    ) -> ProviderPackage:
        """供已创建执行继续运行或取消，不应用新请求禁用策略。"""
        package = (
            self._execution_packages.get(response_id)
            if response_id is not None
            else None
        )
        if package is not None and model not in package.models:
            package = None
        package = package or self._models.get(model) or self._retired_models.get(model)
        if package is None:
            raise LookupError(f"没有注册模型: {model}")
        if id(package) in self._retired_closing_ids:
            raise LookupError(f"Provider 已删除，模型不可用: {model}")
        return package

    def acquire_registered(
        self, model: str, *, response_id: str | None = None
    ) -> ProviderPackage:
        """Acquire a package reference for one admitted execution.

        A package can be retired between request validation and execution
        startup.  Holding this reference makes the hand-off atomic from the
        registry's point of view: retirement may remove the package from new
        routing, but it cannot close the instance while this execution still
        owns it.
        """

        package = self.resolve_registered(model, response_id=response_id)
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

    def bind_execution(self, response_id: str, package: ProviderPackage) -> None:
        """Bind one active response to the package generation that admitted it."""

        if not response_id:
            raise ValueError("response_id 不能为空")
        existing = self._execution_packages.get(response_id)
        if existing is not None and existing is not package:
            raise ValueError(f"响应已绑定到其他 Provider generation: {response_id}")
        self._execution_packages[response_id] = package

    def unbind_execution(self, response_id: str, package: ProviderPackage) -> None:
        """Remove a generation binding only when it still points at package."""

        if self._execution_packages.get(response_id) is package:
            self._execution_packages.pop(response_id, None)

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
                for retired_id, registered in list(self._retired_providers.items()):
                    if registered is package:
                        self._retired_providers.pop(retired_id, None)
                for model, registered in list(self._retired_models.items()):
                    if registered is package:
                        self._retired_models.pop(model, None)

    @property
    def providers(self) -> Mapping[str, ProviderPackage]:
        return dict(self._providers)

    @staticmethod
    def _build_provider_package(
        provider_id: str,
        factory: ProviderFactory,
        package_settings: Mapping[str, Any],
        *,
        creation_tracker: list[ProviderPackage] | None = None,
    ) -> ProviderPackage:
        """Build one package, including the canonical multi-key wrapper."""

        pool = normalize_provider_keys(package_settings)
        if "api_keys" in package_settings and not pool:
            raise ValueError(
                f"Provider {provider_id} 的 api_keys 不能为空；请至少保留一把启用密钥"
            )
        if pool and not any(enabled for _, _, enabled in pool):
            raise ValueError(
                f"Provider {provider_id} 没有启用的 api_keys；请启用至少一把密钥，"
                "或使用 Provider 启停策略"
            )
        factory_settings = _factory_settings(package_settings, pool)
        first_enabled = next((entry for entry in pool if entry[2]), None)

        def create_package(settings: Mapping[str, Any]) -> ProviderPackage:
            package = factory(settings)
            if creation_tracker is not None and isinstance(package, ProviderPackage):
                creation_tracker.append(package)
            return package

        def build_key_package(
            secret: str,
            routed_settings: Mapping[str, Any],
        ) -> ProviderPackage:
            return factory(
                {**dict(routed_settings), "api_key": secret, "api_keys": None}
            )

        def create_key_package(
            secret: str,
            routed_settings: Mapping[str, Any],
        ) -> ProviderPackage:
            return create_package(
                {**dict(routed_settings), "api_key": secret, "api_keys": None}
            )

        try:
            package = create_package(factory_settings)
        except ValueError as exc:
            if len(pool) <= 1 or not _is_missing_provider_key_error(exc):
                raise
            first = next((entry for entry in pool if entry[2]), None)
            if first is None:
                raise
            package = create_key_package(first[1], package_settings)
        # 即使只有一个 legacy api_key，也要包装成可观测的密钥池，
        # 否则 Provider 能调用但 Web 控制台会误显示“尚未配置密钥”。
        if pool and not hasattr(package, "key_statuses"):
            packages: list[tuple[str, ProviderPackage]] = []
            for key_id, secret, enabled in pool:
                if not enabled:
                    continue
                key_package = (
                    package
                    if first_enabled is not None and key_id == first_enabled[0]
                    else create_key_package(secret, package_settings)
                )
                packages.append((key_id, key_package))
            if packages:
                package = RoutedProviderPackage(
                    provider_id,
                    packages,
                    key_secrets={key_id: secret for key_id, secret, _ in pool},
                    configured_keys=[
                        (key_id, enabled) for key_id, _, enabled in pool
                    ],
                    package_factory=build_key_package,
                )
        if not isinstance(package, ProviderPackage):
            raise TypeError(
                f"providers.{provider_id}.create_provider 返回值不符合 ProviderPackage"
            )
        if package.provider_id != provider_id:
            raise ValueError(
                f"Provider ID 必须与目录名一致: 目录={provider_id}, "
                f"provider_id={package.provider_id}"
            )
        return package

    @staticmethod
    async def _close_package_quietly(package: ProviderPackage) -> None:
        try:
            await package.close()
        except Exception:
            logger.warning(
                "Discarded Provider close failed for %s (%s)",
                getattr(package, "provider_id", "unknown"),
                type(package).__name__,
            )

    async def _build_live_candidate(
        self,
        provider_id: str,
        factory: ProviderFactory,
        settings: Mapping[str, Any],
    ) -> ProviderPackage:
        """Build a candidate and close every partial package on failure."""

        created: list[ProviderPackage] = []

        try:
            return self._build_provider_package(
                provider_id,
                factory,
                settings,
                creation_tracker=created,
            )
        except Exception:
            seen: set[int] = set()
            for package in reversed(created):
                if id(package) in seen:
                    continue
                seen.add(id(package))
                await self._close_package_quietly(package)
            raise

    @staticmethod
    async def _validated_catalog(
        package: ProviderPackage,
    ) -> dict[str, dict[str, Any]]:
        """Validate and snapshot a package catalog without calling upstream."""

        models = frozenset(package.models)
        if not models:
            raise ValueError(f"Provider {package.provider_id} 没有注册模型")
        catalog: dict[str, dict[str, Any]] = {}
        prefix = f"{package.provider_id}-"
        for model in sorted(models):
            if not model.startswith(prefix) or len(model) == len(prefix):
                raise ValueError(f"模型必须以 {prefix} 开头且包含模型名: {model}")
            try:
                capability = await package.capabilities(model)
                validated = ModelCapabilities.model_validate(capability)
            except Exception as exc:
                raise ValueError(
                    f"Provider {package.provider_id} 候选能力声明校验失败: "
                    f"{model} ({type(exc).__name__})"
                ) from None
            if validated.model != model:
                raise ValueError(
                    f"Provider {package.provider_id} 能力声明模型名不一致: {model}"
                )
            catalog[model] = validated.model_dump(mode="json")
        return catalog

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
            package = self._build_provider_package(
                short_name, factory, package_settings
            )
            self.register(package, managed=True)
            self._provider_factories[short_name] = factory
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

    def _retire_package(self, package: ProviderPackage) -> None:
        package_id = id(package)
        self._retired_providers[package_id] = package
        self._retired_package_ids.add(package_id)
        for model in package.models:
            if self._models.get(model) is package:
                self._models.pop(model, None)
            self._retired_models[model] = package

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

        rebuilds: dict[
            str, tuple[ProviderPackage, ProviderPackage, dict[str, Any]]
        ] = {}
        reloads: list[
            tuple[str, ProviderPackage, dict[str, Any], dict[str, Any]]
        ] = []
        try:
            for provider_id, package, previous, merged in pending:
                try:
                    rebuild = package.requires_catalog_rebuild(previous, merged)
                except Exception as exc:
                    raise ValueError(
                        f"Provider {provider_id} 模型目录变更判断失败 "
                        f"({type(exc).__name__})"
                    ) from None
                if not isinstance(rebuild, bool):
                    raise TypeError(
                        f"Provider {provider_id} requires_catalog_rebuild 必须返回 bool"
                    )
                if not rebuild:
                    reloads.append((provider_id, package, previous, merged))
                    continue
                factory = self._provider_factories.get(provider_id)
                if factory is None:
                    raise RuntimeError(
                        f"Provider {provider_id} 未由 discover 管理，不能热重建模型目录"
                    )
                try:
                    candidate = await self._build_live_candidate(
                        provider_id, factory, merged
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Provider {provider_id} 候选包构造失败 "
                        f"({type(exc).__name__})"
                    ) from None
                try:
                    await self._validated_catalog(candidate)
                except Exception:
                    await self._close_package_quietly(candidate)
                    raise
                rebuilds[provider_id] = (package, candidate, merged)

            # Validate the complete proposed route table before mutating the
            # active registry.  This catches conflicts between two candidates
            # as well as conflicts with an unchanged Provider.
            proposed_routes: dict[str, str] = {}
            for provider_id, package in self._providers.items():
                if provider_id in removed_provider_ids or provider_id in rebuilds:
                    continue
                for model in package.models:
                    previous_owner = proposed_routes.setdefault(model, provider_id)
                    if previous_owner != provider_id:
                        raise ValueError(
                            f"模型路由重复注册: {model} ({previous_owner}, {provider_id})"
                        )
            for provider_id, (_, candidate, _) in rebuilds.items():
                for model in candidate.models:
                    previous_owner = proposed_routes.setdefault(model, provider_id)
                    if previous_owner != provider_id:
                        raise ValueError(
                            f"模型路由重复注册: {model} ({previous_owner}, {provider_id})"
                        )
        except Exception:
            for _, candidate, _ in rebuilds.values():
                await self._close_package_quietly(candidate)
            raise

        attempted: list[tuple[str, ProviderPackage, dict[str, Any]]] = []
        try:
            for provider_id, package, previous, merged in reloads:
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
            for _, candidate, _ in rebuilds.values():
                await self._close_package_quietly(candidate)
            raise

        for provider_id, _, _, merged in pending:
            self._applied_provider_settings[provider_id] = merged
        retired: list[ProviderPackage] = []
        # Publish all successfully validated candidates without an await in
        # the middle of the route-table transition.  Existing executions own
        # references to the previous package generation and are unaffected.
        for provider_id, (previous, candidate, _) in rebuilds.items():
            self._retire_package(previous)
            retired.append(previous)
            self._providers[provider_id] = candidate
            for model in candidate.models:
                self._models[model] = candidate
        for provider_id in removed_provider_ids:
            package = self._providers.pop(provider_id, None)
            if package is None:
                continue
            self._retire_package(package)
            retired.append(package)
            self._provider_factories.pop(provider_id, None)
            self._applied_provider_settings.pop(provider_id, None)
        for package in retired:
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
                self._execution_packages.clear()
                self._provider_factories.clear()
