"""Kemo 模型和厂商 DTO 之间的映射；不得处理 SSE 信封。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.models import KemoRequest
from core.provider_contract import ProviderException, ProviderResult

from .errors import ExampleErrorMapper
from .usage import ExampleUsageMapper


# 只有厂商文档明确支持的字段才允许透传。
PASSTHROUGH_PROVIDER_OPTIONS = frozenset({"service_tier", "region"})

# 旧客户端兼容字段可以由本包消费，但不能覆盖对应的统一协议字段。
CONSUMED_PROVIDER_OPTIONS = frozenset({"reasoning_effort"})
SUPPORTED_PROVIDER_OPTIONS = (
    PASSTHROUGH_PROVIDER_OPTIONS | CONSUMED_PROVIDER_OPTIONS
)


class ExampleProtocolMapper:
    def __init__(
        self,
        usage: ExampleUsageMapper,
        errors: ExampleErrorMapper,
        *,
        provider_id: str,
    ) -> None:
        self._usage = usage
        self._errors = errors
        self._model_prefix = f"{provider_id}-"

    def to_provider_request(
        self,
        request: KemoRequest,
        *,
        gateway_system_prompt: str,
    ) -> dict[str, Any]:
        options = request.provider_options or {}
        unknown = set(options) - SUPPORTED_PROVIDER_OPTIONS
        if unknown:
            raise ProviderException(
                self._errors.validation_error(
                    f"未知 provider_options: {sorted(unknown)}",
                    details={"unsupported_options": sorted(unknown)},
                )
            )

        # 下列字段名采用 OpenAI-like 示例，复制模板后必须按厂商真实 DTO 修改。
        body: dict[str, Any] = {
            "model": self._resolve_upstream_model(request.model),
            "stream": request.stream,
            "messages": self._build_messages(
                request,
                gateway_system_prompt=gateway_system_prompt,
            ),
        }

        generation = request.generation or {}
        generation_fields = {
            "max_output_tokens": "max_tokens",
            "temperature": "temperature",
            "top_p": "top_p",
            "stop": "stop",
        }
        for kemo_name, vendor_name in generation_fields.items():
            if generation.get(kemo_name) is not None:
                body[vendor_name] = generation[kemo_name]

        if request.tools:
            body["tools"] = self._convert_tools(request.tools)

        reasoning = request.reasoning
        if reasoning is None and options.get("reasoning_effort") is not None:
            # 仅作为旧客户端回退；统一 reasoning 一旦存在就必须优先。
            reasoning = {
                "enabled": str(options["reasoning_effort"]).lower() != "none",
                "effort": str(options["reasoning_effort"]).lower(),
            }
        if reasoning is not None:
            # TODO: 映射成该厂商真实的 thinking/reasoning DTO。
            body["reasoning"] = dict(reasoning)

        for key in PASSTHROUGH_PROVIDER_OPTIONS:
            if key in options:
                body[key] = options[key]
        return body

    def from_provider_response(self, raw: Mapping[str, Any]) -> ProviderResult:
        """示例非流式映射；复制后按厂商真实响应字段修改。"""
        message = raw.get("message") or {}
        if not isinstance(message, Mapping):
            raise ValueError("厂商响应 message 不是对象")

        output: list[dict[str, Any]] = []
        reasoning = message.get("reasoning")
        if reasoning:
            output.append({
                "id": "rs_0",
                "type": "reasoning",
                "status": "completed",
                "content": str(reasoning),
                "metadata": {},
                "extensions": {},
            })

        content = message.get("content")
        tool_calls = message.get("tool_calls") or []
        if content:
            message_item: dict[str, Any] = {
                "id": "msg_0",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "text", "text": str(content)}],
                "metadata": {},
                "extensions": {},
            }
            if not tool_calls:
                message_item["phase"] = "final_answer"
            output.append(message_item)

        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, Mapping):
                raise ValueError("厂商 tool_call 不是对象")
            output.append(
                self.build_tool_call_item(
                    tool_call,
                    item_id=f"call_0_{index}",
                )
            )

        finish_reason = str(raw.get("finish_reason") or "stop")
        status = self._resolve_status(finish_reason, has_tools=bool(tool_calls))
        return ProviderResult(
            status=status,
            output=output,
            usage=self._usage.from_response(raw.get("usage")),
            provider_response_id=str(raw.get("id") or "") or None,
            incomplete_details=(
                {"reason": finish_reason} if status == "incomplete" else None
            ),
        )

    def _build_messages(
        self,
        request: KemoRequest,
        *,
        gateway_system_prompt: str,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_prompt = self._build_system_prompt(
            gateway_system_prompt,
            request.system_prompt,
        )
        if system_prompt:
            # TODO: 若厂商提供独立的最高权限指令字段，应改用该字段。
            messages.append({"role": "system", "content": system_prompt})

        for index, item in enumerate(request.input):
            item_type = item.get("type")
            role = item.get("role")
            if item_type == "message" and role in {"user", "assistant"}:
                messages.append({
                    "role": role,
                    "content": self._extract_text(
                        item.get("content", []),
                        location=f"input[{index}].content",
                    ) or None,
                })
            elif item_type == "reasoning":
                # 默认不把模型私有思考回放给上游；有 Provider State 时按厂商协议处理。
                continue
            elif item_type == "tool_call":
                tool_call = {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": json.dumps(
                            item.get("arguments", {}),
                            ensure_ascii=False,
                        ),
                    },
                }
                # 连续/并行工具调用属于同一条 assistant 消息。
                if messages and messages[-1].get("role") == "assistant":
                    messages[-1].setdefault("tool_calls", []).append(tool_call)
                else:
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call],
                    })
            elif item_type == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "content": self._extract_text(
                        item.get("content", []),
                        location=f"input[{index}].content",
                    ),
                })
            else:
                raise ProviderException(
                    self._errors.validation_error(
                        f"不支持的 input item: {item_type}",
                        details={"input_index": index, "item_type": item_type},
                    )
                )
        return messages

    def _extract_text(self, blocks: Any, *, location: str) -> str:
        if not isinstance(blocks, list):
            raise ProviderException(
                self._errors.validation_error(
                    f"{location} 必须是内容块数组",
                    details={"location": location},
                )
            )
        text: list[str] = []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, Mapping):
                raise ProviderException(
                    self._errors.validation_error(
                        f"{location}[{block_index}] 必须是对象",
                        details={"location": f"{location}[{block_index}]"},
                    )
                )
            block_type = block.get("type")
            if block_type == "text":
                text.append(str(block.get("text") or ""))
            elif block_type == "json":
                text.append(json.dumps(block.get("data"), ensure_ascii=False))
            else:
                # 能力声明不支持的媒体/引用内容不得静默丢弃。
                raise ProviderException(
                    self._errors.validation_error(
                        f"不支持的内容类型: {block_type}",
                        details={
                            "location": f"{location}[{block_index}]",
                            "content_type": block_type,
                        },
                    )
                )
        return "\n".join(text)

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for index, tool in enumerate(tools):
            if tool.get("type") != "function":
                raise ProviderException(
                    self._errors.validation_error(
                        f"不支持的工具类型: {tool.get('type')}",
                        details={"tool_index": index},
                    )
                )
            converted.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                    "strict": bool(tool.get("strict", False)),
                },
            })
        return converted

    def build_tool_call_item(
        self,
        tool_call: Mapping[str, Any],
        *,
        item_id: str,
    ) -> dict[str, Any]:
        """厂商工具调用 → 单个合法 Kemo tool_call item。"""
        function = tool_call.get("function") or {}
        if not isinstance(function, Mapping):
            raise ValueError("厂商 tool_call.function 不是对象")
        call_id = str(tool_call.get("id") or "").strip()
        name = str(function.get("name") or "").strip()
        if not call_id or not name:
            raise ValueError("厂商 tool_call 缺少 id 或 function.name")

        raw_arguments = function.get("arguments", "")
        if isinstance(raw_arguments, Mapping):
            arguments = dict(raw_arguments)
            arguments_raw = json.dumps(arguments, ensure_ascii=False)
            parse_error = None
        else:
            arguments_raw = str(raw_arguments or "")
            try:
                parsed = json.loads(arguments_raw or "{}")
                if not isinstance(parsed, dict):
                    raise ValueError("tool_call arguments 根节点必须是对象")
                arguments = parsed
                parse_error = None
            except (json.JSONDecodeError, ValueError) as exc:
                arguments = {}
                parse_error = {
                    "type": type(exc).__name__,
                    "message": "工具参数不是有效 JSON 对象",
                }

        item: dict[str, Any] = {
            "id": item_id,
            "type": "tool_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "arguments_raw": arguments_raw,
            "status": "completed",
            "metadata": {},
            "extensions": {},
        }
        if parse_error is not None:
            item["parse_error"] = parse_error
        return item

    @staticmethod
    def _build_system_prompt(gateway_prompt: str, request_prompt: str) -> str:
        return "\n\n".join(
            part for part in (gateway_prompt, request_prompt) if part
        )

    @staticmethod
    def _resolve_status(finish_reason: str, *, has_tools: bool) -> str:
        if finish_reason == "tool_calls" and has_tools:
            return "requires_action"
        if finish_reason in {"length", "content_filter", "resource_exhausted"}:
            return "incomplete"
        return "completed"

    def _resolve_upstream_model(self, model: str) -> str:
        if not model.startswith(self._model_prefix) or len(model) == len(self._model_prefix):
            raise ProviderException(
                self._errors.validation_error(
                    f"模型名缺少 {self._model_prefix} 厂商前缀",
                    details={"model": model},
                )
            )
        return model.removeprefix(self._model_prefix)
