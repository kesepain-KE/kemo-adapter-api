"""厂商私有传输层。实际实现可使用 HTTP、SDK 或异步任务 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any


class ExampleClient:
    def __init__(self, *, api_key: str, base_url: str, timeout_seconds: float) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    async def create(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError("发送厂商非流式请求")

    async def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        raise NotImplementedError("连接并迭代厂商原始流")
        yield {}

    async def cancel(self, provider_response_id: str) -> None:
        return None

    async def close(self) -> None:
        return None
