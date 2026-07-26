"""该厂商目录对网关暴露的唯一 Facade。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.models import KemoRequest, ModelCapabilities
from core.provider_contract import (
    ProviderEvent,
    ProviderException,
    ProviderPackage,
    ProviderResult,
    RequestContext,
)
from .capabilities import MODEL_CAPABILITIES
from .client import ExampleClient
from .errors import ExampleErrorMapper
from .protocol import ExampleProtocolMapper
from .streaming import ExampleStreamMapper
from .usage import ExampleUsageMapper


class ExampleProvider(ProviderPackage):
    provider_id = "example"

    def __init__(self, client: ExampleClient) -> None:
        self._client = client
        self._retired_clients: list[ExampleClient] = []
        self._usage = ExampleUsageMapper()
        self._errors = ExampleErrorMapper()
        self._protocol = ExampleProtocolMapper(self._usage, self._errors)
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
        api_key = str(settings.get("api_key", "")).strip()
        if not api_key:
            raise ValueError("Example Provider 缺少 api_key")
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
        return MODEL_CAPABILITIES[model]

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
