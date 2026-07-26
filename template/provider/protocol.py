"""Kemo 模型和厂商 DTO 之间的映射；不得处理 SSE 信封。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models import KemoRequest
from core.provider_contract import ProviderResult
from .usage import ExampleUsageMapper


class ExampleProtocolMapper:
    def __init__(self, usage: ExampleUsageMapper) -> None:
        self._usage = usage

    def to_provider_request(
        self, request: KemoRequest, *, gateway_system_prompt: str
    ) -> dict[str, Any]:
        allowed_options = {"service_tier", "region"}
        unknown = set(request.provider_options) - allowed_options
        if unknown:
            raise ValueError(f"未知 provider_options: {sorted(unknown)}")
        # TODO: 完整映射 system/input/tools/generation/reasoning/output/provider_state。
        return {
            "model": request.model.removeprefix("example/"),
            "stream": request.stream,
            # TODO: 映射到厂商可用的最高权限指令层，不能降为普通 user message。
            "highest_priority_system_prompt": gateway_system_prompt,
            "kemo_system_prompt": request.system_prompt,
            "provider_options": {
                key: request.provider_options[key]
                for key in allowed_options
                if key in request.provider_options
            },
        }

    def from_provider_response(self, raw: Mapping[str, Any]) -> ProviderResult:
        # TODO: 映射完整 Item，组装工具参数并验证 JSON 后再返回。
        return ProviderResult(
            status="completed",
            output=list(raw.get("normalized_output", [])),
            usage=self._usage.from_response(raw.get("usage")),
            provider_response_id=raw.get("id"),
        )
