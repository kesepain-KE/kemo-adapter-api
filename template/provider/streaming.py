"""厂商原始流到无信封 ProviderEvent 的转换。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.provider_contract import ProviderEvent, ProviderEventKind, ProviderResult

from .errors import ExampleErrorMapper
from .protocol import ExampleProtocolMapper
from .usage import ExampleUsageMapper


class ExampleStreamMapper:
    def __init__(
        self,
        usage: ExampleUsageMapper,
        protocol: ExampleProtocolMapper,
        errors: ExampleErrorMapper,
    ) -> None:
        self._usage = usage
        self._protocol = protocol
        self._errors = errors

    async def convert(
        self,
        source: AsyncIterator[Mapping[str, Any]],
    ) -> AsyncIterator[ProviderEvent]:
        """转换示例厂商事件；复制后必须按真实流协议修改字段名。"""
        text_buffer = ""
        reasoning_buffer = ""
        tool_state: dict[int, dict[str, str]] = {}
        tool_items: dict[int, dict[str, Any]] = {}
        final_usage = None
        provider_response_id: str | None = None

        async for raw_event in source:
            if raw_event.get("id"):
                provider_response_id = str(raw_event["id"])
            event_type = raw_event.get("vendor_type")

            if event_type == "text_delta":
                delta = str(raw_event.get("delta") or "")
                text_buffer += delta
                if delta:
                    yield ProviderEvent(
                        kind=ProviderEventKind.TEXT_DELTA,
                        item_id="msg_0",
                        content_index=0,
                        delta=delta,
                        provider_response_id=provider_response_id,
                    )
                continue

            if event_type == "reasoning_delta":
                delta = str(raw_event.get("delta") or "")
                reasoning_buffer += delta
                if delta:
                    yield ProviderEvent(
                        kind=ProviderEventKind.REASONING_CONTENT_DELTA,
                        item_id="rs_0",
                        content_index=0,
                        delta=delta,
                        provider_response_id=provider_response_id,
                    )
                continue

            if event_type == "tool_arguments_delta":
                index = int(raw_event.get("index", 0))
                state = tool_state.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if raw_event.get("call_id"):
                    state["id"] = str(raw_event["call_id"])
                if raw_event.get("name"):
                    state["name"] = str(raw_event["name"])
                delta = str(raw_event.get("delta") or "")
                state["arguments"] += delta
                if delta:
                    yield ProviderEvent(
                        kind=ProviderEventKind.TOOL_ARGUMENTS_DELTA,
                        item_id=f"call_0_{index}",
                        call_id=state["id"] or None,
                        name=state["name"] or None,
                        delta=delta,
                        provider_response_id=provider_response_id,
                    )
                continue

            if event_type == "tool_completed":
                index = int(raw_event.get("index", 0))
                state = tool_state.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if raw_event.get("call_id"):
                    state["id"] = str(raw_event["call_id"])
                if raw_event.get("name"):
                    state["name"] = str(raw_event["name"])
                if raw_event.get("arguments") is not None:
                    state["arguments"] = str(raw_event["arguments"])
                item = self._tool_item(state, index=index)
                tool_items[index] = item
                yield ProviderEvent(
                    kind=ProviderEventKind.TOOL_COMPLETED,
                    item_id=item["id"],
                    call_id=item["call_id"],
                    name=item["name"],
                    item=item,
                    data={"arguments": item["arguments"]},
                    provider_response_id=provider_response_id,
                )
                continue

            if event_type == "usage":
                final_usage = self._usage.from_response(raw_event.get("usage"))
                yield ProviderEvent(
                    kind=ProviderEventKind.USAGE,
                    usage=final_usage,
                    provider_response_id=provider_response_id,
                )
                continue

            if event_type in {"completed", "incomplete", "failed", "cancelled"}:
                # 有些厂商没有独立 tool_completed 事件，必须在终态前补齐。
                for index in sorted(tool_state):
                    if index in tool_items:
                        continue
                    item = self._tool_item(tool_state[index], index=index)
                    tool_items[index] = item
                    yield ProviderEvent(
                        kind=ProviderEventKind.TOOL_COMPLETED,
                        item_id=item["id"],
                        call_id=item["call_id"],
                        name=item["name"],
                        item=item,
                        data={"arguments": item["arguments"]},
                        provider_response_id=provider_response_id,
                    )

                if raw_event.get("usage") is not None:
                    final_usage = self._usage.from_response(raw_event.get("usage"))
                    yield ProviderEvent(
                        kind=ProviderEventKind.USAGE,
                        usage=final_usage,
                        provider_response_id=provider_response_id,
                    )
                usage = final_usage or self._usage.from_response(None)

                output: list[dict[str, Any]] = []
                if reasoning_buffer:
                    output.append({
                        "id": "rs_0",
                        "type": "reasoning",
                        "status": "completed",
                        "content": reasoning_buffer,
                        "metadata": {},
                        "extensions": {},
                    })
                if text_buffer:
                    message_item: dict[str, Any] = {
                        "id": "msg_0",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "text", "text": text_buffer}],
                        "metadata": {},
                        "extensions": {},
                    }
                    if not tool_items:
                        message_item["phase"] = "final_answer"
                    output.append(message_item)
                output.extend(tool_items[index] for index in sorted(tool_items))

                if event_type == "failed":
                    error = self._errors.from_exception(
                        RuntimeError("厂商流返回失败终态")
                    )
                    result = ProviderResult(
                        status="failed",
                        output=output,
                        usage=usage,
                        error=error,
                        provider_response_id=provider_response_id,
                    )
                    yield ProviderEvent(
                        kind=ProviderEventKind.FAILED,
                        result=result,
                        error=error,
                        provider_response_id=provider_response_id,
                    )
                    return

                if event_type == "cancelled":
                    result = ProviderResult(
                        status="cancelled",
                        output=output,
                        usage=usage,
                        provider_response_id=provider_response_id,
                    )
                    yield ProviderEvent(
                        kind=ProviderEventKind.CANCELLED,
                        result=result,
                        provider_response_id=provider_response_id,
                    )
                    return

                if event_type == "incomplete":
                    reason = str(raw_event.get("finish_reason") or "provider_incomplete")
                    result = ProviderResult(
                        status="incomplete",
                        output=output,
                        usage=usage,
                        incomplete_details={"reason": reason},
                        provider_response_id=provider_response_id,
                    )
                    yield ProviderEvent(
                        kind=ProviderEventKind.INCOMPLETE,
                        result=result,
                        provider_response_id=provider_response_id,
                    )
                    return

                status = "requires_action" if tool_items else "completed"
                result = ProviderResult(
                    status=status,
                    output=output,
                    usage=usage,
                    provider_response_id=provider_response_id,
                )
                yield ProviderEvent(
                    kind=ProviderEventKind.COMPLETED,
                    result=result,
                    provider_response_id=provider_response_id,
                )
                return

            # 未知事件不能无声吞掉；复制模板时应明确 keepalive 等例外。
            raise RuntimeError(f"未知厂商流事件类型: {event_type}")

        raise RuntimeError("厂商流缺少统一终态")

    def _tool_item(
        self,
        state: Mapping[str, str],
        *,
        index: int,
    ) -> dict[str, Any]:
        return self._protocol.build_tool_call_item(
            {
                "id": state.get("id", ""),
                "function": {
                    "name": state.get("name", ""),
                    "arguments": state.get("arguments", ""),
                },
            },
            item_id=f"call_0_{index}",
        )
