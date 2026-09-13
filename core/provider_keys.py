"""Provider API key normalization and failover routing.

The public configuration keeps the legacy ``api_key`` field working while
allowing ``api_keys`` to contain an ordered pool.  The pool never exposes key
material in full; only edge-masked previews, stable identifiers and redacted
health counters are returned.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Awaitable, Generic, TypeVar

from core.provider_contract import (
    ProviderEmbeddingResult,
    ProviderEvent,
    ProviderException,
    ProviderPackage,
    ProviderProbeResult,
    ProviderRerankResult,
    ProviderResult,
    RequestContext,
)
from core.secret_preview import mask_secret


T = TypeVar("T")

_MAX_RESPONSE_BINDINGS = 4096


def preview_provider_key(secret: str | None) -> str | None:
    """Display first five / last three characters; absent credentials stay None."""
    return mask_secret(secret) if secret else None


@dataclass(slots=True)
class ProviderKeyState:
    key_id: str
    api_key: str = field(repr=False)
    enabled: bool = True
    status: str = "healthy"
    calls: int = 0
    successes: int = 0
    failures: int = 0
    last_error_code: str | None = None
    last_used_at: float | None = None
    cooldown_until: float = 0.0

    def public(self) -> dict[str, Any]:
        now = time.time()
        status = self.status
        if self.enabled and self.cooldown_until > now:
            status = "cooldown"
        elif self.enabled and status == "cooldown":
            status = "healthy"
        return {
            "key_id": self.key_id,
            "key_preview": preview_provider_key(self.api_key),
            "status": status if self.enabled else "disabled",
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "last_error_code": self.last_error_code,
            "last_used_at": self.last_used_at,
        }


def normalize_provider_keys(settings: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    """Return validated ``(key_id, secret, enabled)`` entries in order.

    Invalid entries must fail closed.  Silently dropping a malformed key can
    make a live configuration appear healthy while routing only a subset of
    the pool, which is especially dangerous during a key rotation.
    """
    raw = settings.get("api_keys")
    entries: list[Any]
    if raw is None:
        entries = []
    elif isinstance(raw, Mapping):
        entries = []
        for key_id, value in raw.items():
            if isinstance(value, Mapping):
                entries.append({**dict(value), "key_id": key_id})
            elif isinstance(value, str):
                entries.append({"key_id": key_id, "api_key": value})
            else:
                raise ValueError("Provider api_keys 对象包含无效项")
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("Provider api_keys 必须是数组或对象")
    legacy = str(settings.get("api_key", "")).strip()
    # Once an explicit canonical pool exists, it is the only source of truth.
    # An empty pool must not resurrect a stale legacy top-level key.
    if legacy and raw is None:
        entries = [{"key_id": str(settings.get("api_key_id") or "primary"), "api_key": legacy}]
    result: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            secret = entry.strip()
            key_id = f"key-{index + 1}"
            enabled = True
        elif isinstance(entry, Mapping):
            secret = str(entry.get("api_key") or entry.get("key") or "").strip()
            key_id = str(entry.get("key_id") or entry.get("name") or f"key-{index + 1}").strip()
            enabled_value = entry.get("enabled", True)
            if not isinstance(enabled_value, bool):
                raise ValueError("Provider api_keys.enabled 必须是布尔值")
            enabled = enabled_value
        else:
            raise ValueError("Provider api_keys 包含无效项")
        if not secret:
            raise ValueError("Provider api_keys.api_key 不能为空")
        if not key_id:
            raise ValueError("Provider api_keys.key_id 不能为空")
        if key_id in seen:
            raise ValueError("Provider api_keys.key_id 必须唯一")
        seen.add(key_id)
        result.append((key_id, secret, enabled))
    return result


def _is_key_specific_failure(exc: Exception) -> tuple[bool, int | None, str | None]:
    if not isinstance(exc, ProviderException):
        return False, None, None
    error = exc.error
    status = error.provider_status
    code = str(error.code or "").upper()
    message = str(error.message or "").casefold()
    key_codes = {
        "AUTHENTICATION_ERROR",
        "API_KEY_INVALID",
        "INVALID_API_KEY",
        "INSUFFICIENT_QUOTA",
        "INSUFFICIENT_BALANCE",
        "QUOTA_EXCEEDED",
        "CREDIT_EXHAUSTED",
        "RATE_LIMITED",
        "RATE_LIMIT_EXCEEDED",
        "TOO_MANY_REQUESTS",
    }
    key_phrases = (
        "api key",
        "apikey",
        "rate limit",
        "quota exceeded",
        "quota exhausted",
        "insufficient quota",
        "insufficient balance",
        "insufficient credit",
        "credit exhausted",
        "配额不足",
        "额度不足",
        "余额不足",
        "密钥无效",
        "鉴权失败",
        "限流",
    )
    explicit_key_failure = error.details.get("key_failure") is True
    status_key_failure = status in {401, 402, 429}
    # HTTP 403 is ambiguous: it can mean an invalid key, but it can also mean
    # that this model/account is not permitted.  Only fail over when the
    # Provider marks it explicitly or the mapped code/message identifies the
    # credential as the cause.
    status_403_key_failure = status == 403 and (
        explicit_key_failure
        or code in {"AUTHENTICATION_ERROR", "API_KEY_INVALID", "INVALID_API_KEY"}
        or any(phrase in message for phrase in key_phrases)
    )
    failover = (
        status_key_failure
        or status_403_key_failure
        or code in key_codes
        or any(phrase in message for phrase in key_phrases)
    )
    return failover, status, code or None


def is_key_specific_failure(exc: Exception) -> tuple[bool, int | None, str | None]:
    return _is_key_specific_failure(exc)


class RoutedProviderPackage(ProviderPackage):
    """Generic failover facade for Provider packages that have not implemented a pool."""

    def __init__(
        self,
        provider_id: str,
        packages: list[tuple[str, ProviderPackage]],
        *,
        key_secrets: Mapping[str, str] | None = None,
        configured_keys: Sequence[tuple[str, bool]] | None = None,
        package_factory: Callable[[str, Mapping[str, Any]], ProviderPackage] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._packages = packages
        self._cursor = 0
        self._key_secrets = {
            str(key_id): str(secret)
            for key_id, secret in (key_secrets or {}).items()
            if str(secret)
        }
        self._package_factory = package_factory
        self._retired_packages: list[ProviderPackage] = []
        # Preserve the package selected for each known provider response so a
        # later cancellation cannot follow the round-robin cursor to another
        # upstream credential.
        self._response_packages: dict[str, ProviderPackage] = {}
        self._active_calls = 0
        self._closed = False
        self._configured_ids = [key_id for key_id, _ in (configured_keys or [])]
        self._states: dict[str, ProviderKeyState] = {}
        for key_id, enabled in configured_keys or []:
            self._states[key_id] = ProviderKeyState(
                key_id,
                self._key_secrets.get(key_id, ""),
                enabled=enabled,
            )
        for key_id, _ in packages:
            if key_id not in self._states:
                self._configured_ids.append(key_id)
                self._states[key_id] = ProviderKeyState(
                    key_id,
                    self._key_secrets.get(key_id, ""),
                )

    @property
    def models(self) -> frozenset[str]:
        return self._packages[0][1].models

    async def capabilities(self, model: str):
        return await self._packages[0][1].capabilities(model)

    def diagnostics(self) -> Mapping[str, Any]:
        base = dict(self._packages[0][1].diagnostics())
        base["key_statuses"] = self.key_statuses()
        return base

    def key_statuses(self) -> list[dict[str, Any]]:
        return [self._states[key_id].public() for key_id in self._configured_ids]

    def _ordered(self) -> list[tuple[str, ProviderPackage]]:
        if not self._packages:
            raise RuntimeError(f"{self.provider_id} 密钥路由器已关闭")
        ordered = self._packages[self._cursor % len(self._packages):] + self._packages[: self._cursor % len(self._packages)]
        active = [(key_id, package) for key_id, package in ordered if self._states[key_id].enabled]
        available = [
            (key_id, package)
            for key_id, package in active
            if self._states[key_id].cooldown_until <= time.time()
        ]
        return available or active

    def _advance_cursor(self, key_id: str) -> None:
        for index, item in enumerate(self._packages):
            if item[0] == key_id:
                self._cursor = (index + 1) % len(self._packages)
                return

    @staticmethod
    def _response_id(value: Any) -> str | None:
        if isinstance(value, Mapping):
            for key in ("provider_response_id", "response_id", "task_id"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate)
            nested = value.get("response")
            if isinstance(nested, Mapping):
                candidate = nested.get("id") or nested.get("response_id")
                if candidate:
                    return str(candidate)
            candidate = value.get("id")
            if candidate:
                return str(candidate)
            return None
        for key in ("provider_response_id", "response_id"):
            candidate = getattr(value, key, None)
            if candidate:
                return str(candidate)
        return None

    def _remember_response(self, value: Any, package: ProviderPackage) -> None:
        response_id = self._response_id(value)
        if response_id:
            if response_id not in self._response_packages and len(self._response_packages) >= _MAX_RESPONSE_BINDINGS:
                self._response_packages.pop(next(iter(self._response_packages)), None)
            self._response_packages[response_id] = package

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        last: Exception | None = None
        self._active_calls += 1
        try:
            for key_id, package in self._ordered():
                # Reserve the next round-robin position before awaiting the
                # upstream.  Otherwise concurrent calls can all observe the
                # same cursor while the first request is still in flight.
                self._advance_cursor(key_id)
                state = self._states[key_id]
                state.calls += 1
                state.last_used_at = time.time()
                try:
                    value = await getattr(package, method)(*args, **kwargs)
                except Exception as exc:
                    last = exc
                    state.failures += 1
                    failover, status, code = is_key_specific_failure(exc)
                    state.last_error_code = code
                    if not failover:
                        raise
                    state.status = "exhausted" if status in {401, 402, 403} else "cooldown"
                    state.cooldown_until = time.time() + (300 if status in {401, 402, 403} else 30)
                    continue
                state.successes += 1
                state.status = "healthy"
                state.last_error_code = None
                self._remember_response(value, package)
                return value
            if last is not None:
                raise last
            raise RuntimeError(f"{self.provider_id} 没有可用密钥")
        finally:
            self._active_calls -= 1
            if self._active_calls == 0:
                await self._drain_retired()

    async def probe(self, model: str, context: RequestContext) -> ProviderProbeResult:
        return await self._call("probe", model, context)

    async def execute(self, request: Any, context: RequestContext) -> ProviderResult:
        return await self._call("execute", request, context)

    def stream(self, request: Any, context: RequestContext) -> AsyncIterator[ProviderEvent]:
        async def generate() -> AsyncIterator[ProviderEvent]:
            last: Exception | None = None
            self._active_calls += 1
            try:
                for key_id, package in self._ordered():
                    self._advance_cursor(key_id)
                    state = self._states[key_id]
                    state.calls += 1
                    state.last_used_at = time.time()
                    emitted = False
                    try:
                        async for event in package.stream(request, context):
                            emitted = True
                            self._remember_response(event, package)
                            yield event
                        state.successes += 1
                        state.status = "healthy"
                        state.last_error_code = None
                        return
                    except Exception as exc:
                        last = exc
                        state.failures += 1
                        failover, status, code = is_key_specific_failure(exc)
                        state.last_error_code = code
                        if emitted or not failover:
                            raise
                        state.status = "exhausted" if status in {401, 402, 403} else "cooldown"
                        state.cooldown_until = time.time() + (300 if status in {401, 402, 403} else 30)
                if last is not None:
                    raise last
                raise RuntimeError(f"{self.provider_id} 没有可用密钥")
            finally:
                self._active_calls -= 1
                if self._active_calls == 0:
                    await self._drain_retired()
        return generate()

    async def embed(self, request: Any, context: RequestContext) -> ProviderEmbeddingResult:
        return await self._call("embed", request, context)

    async def rerank(self, request: Any, context: RequestContext) -> ProviderRerankResult:
        return await self._call("rerank", request, context)

    async def cancel(self, provider_response_id: str | None, context: RequestContext) -> None:
        if not self._packages:
            return
        response_key = str(provider_response_id) if provider_response_id else None
        package = self._response_packages.get(response_key) if response_key else None
        if package is None:
            # Never send a cancellation to an arbitrary current package: the
            # round-robin cursor may now point at a different upstream key.
            # Unknown IDs are best-effort no-ops; the local gateway still
            # records cancellation and its task lease will expire.
            return
        await package.cancel(provider_response_id, context)
        if response_key:
            self._response_packages.pop(response_key, None)

    async def reload_config(self, settings: Mapping[str, Any]) -> None:
        entries = normalize_provider_keys(settings)
        if not entries:
            raise ValueError(f"{self.provider_id} 缺少 api_keys；至少保留一把启用密钥")
        if entries and not any(enabled for _, _, enabled in entries):
            raise ValueError(
                f"{self.provider_id} 没有启用的 api_keys；请启用至少一把密钥，"
                "或使用 Provider 启停策略"
            )
        old_packages = self._packages
        if self._package_factory is not None:
            enabled_entries = [(key_id, secret) for key_id, secret, enabled in entries if enabled]
            if enabled_entries:
                rebuilt: list[tuple[str, ProviderPackage]] = []
                try:
                    rebuilt = [
                        (key_id, self._package_factory(secret, settings))
                        for key_id, secret in enabled_entries
                    ]
                except Exception:
                    for _, package in rebuilt:
                        await package.close()
                    raise
                current_key_id = (
                    old_packages[self._cursor % len(old_packages)][0]
                    if old_packages
                    else None
                )
                self._packages = rebuilt
                self._cursor = next(
                    (index for index, (key_id, _) in enumerate(rebuilt) if key_id == current_key_id),
                    0,
                )
                self._retired_packages.extend(package for _, package in old_packages)
        self._key_secrets = {key_id: secret for key_id, secret, _ in entries}
        self._configured_ids = [key_id for key_id, _, _ in entries]
        for key_id, secret, enabled in entries:
            state = self._states.get(key_id)
            if state is None:
                self._states[key_id] = ProviderKeyState(key_id, secret, enabled=enabled)
            elif state.api_key != secret:
                # A replaced secret is a new credential.  Replace the state
                # object instead of mutating it so in-flight calls retain the
                # health counters and cooldown belonging to the old secret.
                self._states[key_id] = ProviderKeyState(key_id, secret, enabled=enabled)
            else:
                state.enabled = enabled
        for key_id, state in self._states.items():
            if key_id not in self._configured_ids:
                # Keep the state object long enough for any package instance
                # still serving an in-flight request, but never route new
                # calls through a removed key.
                state.enabled = False
        if self._package_factory is None:
            active_entries = {key_id: secret for key_id, secret, enabled in entries if enabled}
            for key_id, package in self._packages:
                secret = active_entries.get(key_id)
                if secret is not None:
                    await package.reload_config({**dict(settings), "api_key": secret, "api_keys": None})
        if self._active_calls == 0:
            await self._drain_retired()

    async def _drain_retired(self) -> None:
        retired, self._retired_packages = self._retired_packages, []
        for package in retired:
            try:
                await package.close()
            except Exception:
                # A retired package must never turn a successful request into
                # a failure merely because its idle connection pool closed
                # noisily.
                continue
            self._response_packages = {
                response_id: mapped
                for response_id, mapped in self._response_packages.items()
                if mapped is not package
            }
        if self._closed and self._active_calls == 0:
            self._packages = []
            self._response_packages.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._retired_packages.extend(package for _, package in self._packages)
        if self._active_calls == 0:
            self._packages = []
            await self._drain_retired()


class ProviderKeyRouter(Generic[T]):
    """Ordered key failover for one Provider without leaking key material."""

    def __init__(self, provider_id: str, clients: list[tuple[str, T]]) -> None:
        self.provider_id = provider_id
        self._clients = clients
        self._states = {key_id: ProviderKeyState(key_id, "") for key_id, _ in clients}
        self._configured_ids = [key_id for key_id, _ in clients]
        self._client_by_key = dict(clients)
        self._cursor = 0
        self._active_calls = 0
        self._retired_clients: list[T] = []
        # A provider response can outlive the round-robin cursor (especially
        # while a stream is being cancelled).  Keep the client that created
        # each known response so cancel never lands on a different API key.
        self._response_clients: dict[str, T] = {}
        self._closed = False

    @classmethod
    def build(
        cls,
        provider_id: str,
        settings: Mapping[str, Any],
        factory: Callable[[str], T],
    ) -> "ProviderKeyRouter[T]":
        entries = normalize_provider_keys(settings)
        if not entries:
            raise ValueError(f"{provider_id} Provider 缺少 api_key")
        clients = []
        router = cls(provider_id, [])
        for key_id, secret, enabled in entries:
            router._configured_ids.append(key_id)
            router._states[key_id] = ProviderKeyState(key_id, secret, enabled=enabled)
            if not enabled:
                continue
            client = factory(secret)
            clients.append((key_id, client))
        if not clients:
            raise ValueError(f"{provider_id} 没有启用的 api_keys")
        router._clients = clients
        router._client_by_key = dict(clients)
        return router

    @property
    def active_client(self) -> T:
        if not self._clients:
            raise RuntimeError(f"{self.provider_id} 密钥路由器已关闭")
        return self._clients[self._cursor % len(self._clients)][1]

    def statuses(self) -> list[dict[str, Any]]:
        return [self._states[key_id].public() for key_id in self._configured_ids]

    def _order(self) -> list[tuple[str, T]]:
        if not self._clients:
            raise RuntimeError(f"{self.provider_id} 密钥路由器已关闭")
        now = time.time()
        ordered = self._clients[self._cursor % len(self._clients):] + self._clients[: self._cursor % len(self._clients)]
        active = [(key_id, client) for key_id, client in ordered if self._states[key_id].enabled]
        available = [
            (key_id, client)
            for key_id, client in active
            if self._states[key_id].cooldown_until <= now
        ]
        return available or active

    def _advance_cursor(self, key_id: str) -> None:
        for index, item in enumerate(self._clients):
            if item[0] == key_id:
                self._cursor = (index + 1) % len(self._clients)
                return

    @staticmethod
    def _response_id(value: Any) -> str | None:
        """Extract a provider response/task id from common provider shapes."""
        if isinstance(value, Mapping):
            for key in ("provider_response_id", "response_id", "task_id"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate)
            nested = value.get("response")
            if isinstance(nested, Mapping):
                candidate = nested.get("id") or nested.get("response_id")
                if candidate:
                    return str(candidate)
            candidate = value.get("id")
            if candidate:
                return str(candidate)
            return None
        for key in ("provider_response_id", "response_id"):
            candidate = getattr(value, key, None)
            if candidate:
                return str(candidate)
        return None

    def _remember_response(self, value: Any, client: T) -> None:
        response_id = self._response_id(value)
        if response_id:
            if response_id not in self._response_clients and len(self._response_clients) >= _MAX_RESPONSE_BINDINGS:
                self._response_clients.pop(next(iter(self._response_clients)), None)
            self._response_clients[response_id] = client

    async def call_with(
        self,
        operation: Callable[[T], Awaitable[Any]],
    ) -> Any:
        """Run an arbitrary operation against the routed client pool.

        Provider-specific operations (for example audio/image endpoints) can
        use the same key failover policy without exposing key material or
        reaching into the router's private client list.
        """
        last: Exception | None = None
        self._active_calls += 1
        try:
            for key_id, client in self._order():
                # Move the cursor before the first await so another concurrent
                # request starts from the next credential instead of piling
                # onto the same key.
                self._advance_cursor(key_id)
                state = self._states[key_id]
                state.calls += 1
                state.last_used_at = time.time()
                try:
                    result = await operation(client)
                except Exception as exc:
                    last = exc
                    state.failures += 1
                    failover, status, code = _is_key_specific_failure(exc)
                    state.last_error_code = code
                    if not failover:
                        raise
                    state.status = "exhausted" if status in {402, 401, 403} else "cooldown"
                    state.cooldown_until = time.time() + (300 if status in {401, 402, 403} else 30)
                    continue
                state.successes += 1
                state.status = "healthy"
                state.last_error_code = None
                self._remember_response(result, client)
                return result
            if last is not None:
                raise last
            raise RuntimeError(f"{self.provider_id} 没有可用密钥")
        finally:
            self._active_calls -= 1
            if self._active_calls == 0:
                await self._drain_retired()

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await self.call_with(
            lambda client: getattr(client, method)(*args, **kwargs)
        )

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        last: Exception | None = None
        self._active_calls += 1
        try:
            for key_id, client in self._order():
                self._advance_cursor(key_id)
                state = self._states[key_id]
                state.calls += 1
                state.last_used_at = time.time()
                emitted = False
                try:
                    async for item in getattr(client, "stream")(*args, **kwargs):
                        emitted = True
                        self._remember_response(item, client)
                        yield item
                    state.successes += 1
                    state.status = "healthy"
                    state.last_error_code = None
                    return
                except Exception as exc:
                    last = exc
                    state.failures += 1
                    failover, status, code = _is_key_specific_failure(exc)
                    state.last_error_code = code
                    if emitted or not failover:
                        raise
                    state.status = "exhausted" if status in {401, 402, 403} else "cooldown"
                    state.cooldown_until = time.time() + (300 if status in {401, 402, 403} else 30)
                    continue
            if last is not None:
                raise last
            raise RuntimeError(f"{self.provider_id} 没有可用密钥")
        finally:
            self._active_calls -= 1
            if self._active_calls == 0:
                await self._drain_retired()

    async def cancel(self, provider_response_id: str | None) -> None:
        if not provider_response_id:
            return
        response_key = str(provider_response_id)
        client = self._response_clients.get(response_key)
        if client is None:
            # Avoid cancelling a different credential's task after a reload
            # or process recovery when the original mapping is unavailable.
            return
        cancel = getattr(client, "cancel", None)
        if cancel is not None:
            await cancel(response_key)
        self._response_clients.pop(response_key, None)

    async def _drain_retired(self) -> None:
        retired, self._retired_clients = self._retired_clients, []
        for client in retired:
            try:
                await client.close()
            except Exception:
                continue
            self._response_clients = {
                response_id: mapped
                for response_id, mapped in self._response_clients.items()
                if mapped is not client
            }
        if self._closed and self._active_calls == 0:
            self._clients = []
            self._client_by_key = {}
            self._response_clients.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._retired_clients.extend(client for _, client in self._clients)
        if self._active_calls == 0:
            self._clients = []
            self._client_by_key = {}
            await self._drain_retired()
