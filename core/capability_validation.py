"""在请求进入 Provider 前校验其公开能力声明。

核心只检查统一 Kemo 能力，不理解任何厂商字段或端点。Provider 仍负责厂商协议转换和更细的
参数校验。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models import ErrorObject, KemoRequest, ModelCapabilities
from core.provider_contract import ProviderException


_TEXT_CONTENT_TYPES = frozenset({"text", "json"})
_KNOWN_CONTENT_MODALITIES = frozenset(
    {"image", "audio", "video", "file", "reference"}
)


def validate_llm_request_capabilities(
    request: KemoRequest,
    capabilities: ModelCapabilities,
) -> None:
    """依据 Provider 的声明验证统一 LLM 请求，失败时返回稳定脱敏错误。"""
    if capabilities.model != request.model:
        _fail(
            "CAPABILITIES_MODEL_MISMATCH",
            "Provider 返回的能力声明与请求模型不一致。",
            details={
                "request_model": request.model,
                "capabilities_model": capabilities.model,
            },
        )
    if capabilities.task != "llm":
        _fail(
            "MODEL_TASK_MISMATCH",
            "该模型不是 LLM，不能通过 /model/responses 调用。",
            details={"model": request.model, "task": capabilities.task},
        )

    requested_inputs = _request_input_modalities(request.input)
    unsupported_inputs = requested_inputs - set(capabilities.input_modalities)
    if unsupported_inputs:
        _fail(
            "UNSUPPORTED_INPUT_MODALITY",
            f"模型不支持输入模态: {sorted(unsupported_inputs)}",
            details={
                "model": request.model,
                "requested_modalities": sorted(requested_inputs),
                "supported_modalities": sorted(capabilities.input_modalities),
            },
        )

    requested_outputs = _request_output_modalities(request.output)
    unsupported_outputs = requested_outputs - set(capabilities.output_modalities)
    if unsupported_outputs:
        _fail(
            "UNSUPPORTED_OUTPUT_MODALITY",
            f"模型不支持输出模态: {sorted(unsupported_outputs)}",
            details={
                "model": request.model,
                "requested_modalities": sorted(requested_outputs),
                "supported_modalities": sorted(capabilities.output_modalities),
            },
        )

    if request.stream and not capabilities.streaming:
        _fail(
            "STREAMING_UNSUPPORTED",
            "该模型未声明支持流式响应。",
            details={"model": request.model},
        )
    if request.tools and not capabilities.tools.function_calling:
        _fail(
            "TOOLS_UNSUPPORTED",
            "该模型未声明支持工具调用。",
            details={"model": request.model},
        )
    if (
        request.tools
        and bool((request.generation or {}).get("parallel_tool_calls", True))
        and not capabilities.tools.parallel_calls
    ):
        _fail(
            "PARALLEL_TOOLS_UNSUPPORTED",
            "该模型未声明支持并行工具调用。",
            details={"model": request.model},
        )

    reasoning = request.reasoning or {}
    if bool(reasoning.get("enabled", False)):
        if not capabilities.reasoning.supported:
            _fail(
                "REASONING_UNSUPPORTED",
                "该模型未声明支持推理模式。",
                details={"model": request.model},
            )
        effort = str(reasoning.get("effort") or "").strip().lower()
        supported_efforts = set(capabilities.reasoning.efforts)
        if effort and effort != "none" and effort not in supported_efforts:
            _fail(
                "REASONING_EFFORT_UNSUPPORTED",
                f"模型不支持推理档位: {effort}",
                details={
                    "model": request.model,
                    "effort": effort,
                    "supported_efforts": sorted(supported_efforts),
                },
            )


def _request_input_modalities(items: list[dict[str, Any]]) -> set[str]:
    modalities: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type in _TEXT_CONTENT_TYPES:
                modalities.add("text")
            elif block_type in _KNOWN_CONTENT_MODALITIES:
                modalities.add(block_type)
    return modalities


def _request_output_modalities(output: Mapping[str, Any]) -> set[str]:
    raw = output.get("modalities")
    if not isinstance(raw, list) or not raw:
        return {"text"}
    return {str(value).strip() for value in raw if str(value).strip()}


def _fail(code: str, message: str, *, details: dict[str, Any]) -> None:
    raise ProviderException(
        ErrorObject(
            type="capability_validation",
            code=code,
            message=message,
            retryable=False,
            details=details,
        )
    )
