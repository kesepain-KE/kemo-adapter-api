"""厂商私有传输层。实际实现可使用 HTTP、SDK 或异步任务 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any


class ExampleClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        default_headers: Mapping[str, object] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Provider API Key 不能为空")
        if not base_url.strip():
            raise ValueError("Provider Base URL 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("Provider timeout_seconds 必须大于 0")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        default_header_values = {
            str(name): str(value)
            for name, value in (default_headers or {}).items()
            if str(name).strip()
        }
        protected_headers = {
            name for name in default_header_values
            if name.lower() in {"authorization", "proxy-authorization", "x-api-key"}
        }
        if protected_headers:
            raise ValueError("default_headers 不得覆盖厂商鉴权 Header")
        self._default_headers = default_header_values
        # TODO: 建立 Client 时由 api_key 生成鉴权 Header；日志和异常不得包含该值。

    async def create(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError("发送厂商非流式请求")

    async def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        raise NotImplementedError("连接并迭代厂商原始流")
        yield {}

    async def cancel(self, provider_response_id: str) -> None:
        return None

    async def close(self) -> None:
        return None
