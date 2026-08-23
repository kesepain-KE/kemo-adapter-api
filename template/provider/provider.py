"""该厂商目录对网关暴露的唯一 Facade。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.models import KemoRequest, ModelCapabilities
from core.provider_contract import (
    ProviderEvent,
    ProviderException,
    ProviderPackage,
    ProviderProbeResult,
    ProviderResult,
    RequestContext,
)
from .capabilities import MODEL_CAPABILITIES
from .client import ExampleClient
from .errors import ExampleErrorMapper
from .protocol import ExampleProtocolMapper
from .probe import probe_model
from .streaming import ExampleStreamMapper
from .usage import ExampleUsageMapper


PROVIDER_KEY_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def resolve_api_key(settings: Mapping[str, Any]) -> str:
    """Resolve one upstream key without exposing key material.

    The registry may inject a single ``api_key`` when it is constructing a
    per-key package.  That explicit value always wins.  A standalone Provider
    factory invocation can instead receive the canonical ``api_keys`` array;
    in that case the first enabled, non-empty entry is selected.  The legacy
    single-key configuration remains supported when no pool is supplied.
    """

    explicit = str(settings.get("api_key") or "").strip()
    if explicit:
        return explicit

    raw_pool = settings.get("api_keys")
    if raw_pool is None:
        raise ValueError("Example Provider 缺少 api_key")
    if not isinstance(raw_pool, list):
        raise ValueError("Example Provider 的 api_keys 必须是数组")

    if not raw_pool:
        raise ValueError("Example Provider 的 api_keys 不能为空")

    seen_ids: set[str] = set()
    selected: str | None = None
    for entry in raw_pool:
        if not isinstance(entry, Mapping):
            raise ValueError("Example Provider 的 api_keys 包含无效项")
        key_id = str(entry.get("key_id") or "").strip()
        if not key_id or not PROVIDER_KEY_ID.fullmatch(key_id):
            raise ValueError("Example Provider 的 api_keys.key_id 无效")
        if key_id in seen_ids:
            raise ValueError("Example Provider 的 api_keys.key_id 必须唯一")
        seen_ids.add(key_id)
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("Example Provider 的 api_keys.enabled 必须是布尔值")
        candidate = str(entry.get("api_key") or "").strip()
        if not candidate:
            raise ValueError("Example Provider 的 api_keys.api_key 不能为空")
        if enabled and selected is None:
            selected = candidate

    if selected is not None:
        return selected
    raise ValueError("Example Provider 没有启用的 api_keys")


class ExampleProvider(ProviderPackage):
    provider_id = "example"

    def __init__(self, client: ExampleClient) -> None:
        self._client = client
        self._retired_clients: list[ExampleClient] = []
        self._usage = ExampleUsageMapper()
        self._errors = ExampleErrorMapper()
        self._protocol = ExampleProtocolMapper(
            self._usage,
            self._errors,
            provider_id=self.provider_id,
        )
        self._streaming = ExampleStreamMapper(
            self._usage,
            self._protocol,
            self._errors,
        )

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "ExampleProvider":
        # 实际包应在启动时快速校验缺失配置，但不得输出密钥值。
        return cls(cls._client_from_settings(settings))

    @staticmethod
    def _client_from_settings(settings: Mapping[str, Any]) -> ExampleClient:
        api_key = resolve_api_key(settings)
        headers = settings.get("default_headers")
        return ExampleClient(
            api_key=api_key,
            base_url=str(settings.get("base_url", "https://api.example.invalid")),
            timeout_seconds=float(settings.get("timeout_seconds", 120)),
            default_headers=headers if isinstance(headers, Mapping) else None,
        )

    @property
    def models(self) -> frozenset[str]:
        return frozenset(MODEL_CAPABILITIES)

    async def capabilities(self, model: str) -> ModelCapabilities:
        try:
            return MODEL_CAPABILITIES[model]
        except KeyError as exc:
            raise LookupError(f"{self.provider_id} 未知模型: {model}") from exc

    async def probe(self, model: str, context: RequestContext) -> ProviderProbeResult:
        await self.capabilities(model)
        return await probe_model(model, context, self.execute)

    async def execute(self, request: KemoRequest, context: RequestContext) -> ProviderResult:
        try:
            payload = self._protocol.to_provider_request(
                request, gateway_system_prompt=context.gateway_system_prompt
            )
            raw = await self._client.create(payload)
            return self._protocol.from_provider_response(raw)
        except ProviderException:
            raise
        except Exception as exc:
            raise ProviderException(self._errors.from_exception(exc)) from exc

    async def _stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        try:
            payload = self._protocol.to_provider_request(
                request, gateway_system_prompt=context.gateway_system_prompt
            )
            async for event in self._streaming.convert(self._client.stream(payload)):
                yield event
        except ProviderException:
            raise
        except Exception as exc:
            raise ProviderException(self._errors.from_exception(exc)) from exc

    def stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        return self._stream(request, context)

    async def cancel(self, provider_response_id: str | None, context: RequestContext) -> None:
        del context
        if provider_response_id is not None:
            await self._client.cancel(provider_response_id)

    async def reload_config(self, settings: Mapping[str, Any]) -> None:
        """新请求使用新 Client；旧 Client 保留到进程退出，避免打断在途请求。"""
        replacement = self._client_from_settings(settings)
        previous = self._client
        self._client = replacement
        self._retired_clients.append(previous)

    async def close(self) -> None:
        await self._client.close()
        for client in self._retired_clients:
            await client.close()
