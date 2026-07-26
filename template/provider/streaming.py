"""厂商原始流到无信封 ProviderEvent 的转换。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderResult
from .usage import ExampleUsageMapper


class ExampleStreamMapper:
    def __init__(self, usage: ExampleUsageMapper) -> None:
        self._usage = usage

    async def convert(
        self, source: AsyncIterator[Mapping[str, Any]]
    ) -> AsyncIterator[ProviderEvent]:
        final_result: ProviderResult | None = None
        async for raw_event in source:
            event_type = raw_event.get("vendor_type")
            if event_type == "text_delta":
                yield ProviderEvent(
                    kind=ProviderEventKind.TEXT_DELTA,
                    item_id=str(raw_event["item_id"]),
                    content_index=int(raw_event.get("content_index", 0)),
                    delta=str(raw_event["delta"]),
                )
            elif event_type == "usage":
                yield ProviderEvent(
                    kind=ProviderEventKind.USAGE,
                    usage=self._usage.from_response(raw_event.get("usage")),
                )
            elif event_type == "completed":
                final_result = ProviderResult(
                    status="completed",
                    output=list(raw_event.get("normalized_output", [])),
                    usage=self._usage.from_response(raw_event.get("usage")),
                    provider_response_id=raw_event.get("id"),
                )
                yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=final_result)
            else:
                # TODO: 映射 reasoning/tool/media/error/cancel/incomplete，未知事件必须有明确策略。
                continue

        if final_result is None:
            raise RuntimeError("厂商流缺少终态")
