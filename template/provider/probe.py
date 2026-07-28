"""厂商模型可达性探测；每类模型必须按厂商真实协议实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from core.models import KemoRequest
from core.provider_contract import ProviderProbeResult, ProviderResult, RequestContext


ProbeExecutor = Callable[[KemoRequest, RequestContext], Awaitable[ProviderResult]]


async def probe_model(
    model: str,
    context: RequestContext,
    execute: ProbeExecutor,
) -> ProviderProbeResult:
    """LLM 模板：执行低成本最小生成；其他任务必须替换为对应探测协议。"""
    request = KemoRequest(
        protocol_version="1.0",
        request_id=context.request_id,
        attempt=1,
        model=model,
        stream=False,
        system_prompt="这是一次连通性检查。只回复 OK，不要解释。",
        generation={"max_output_tokens": 64},
        output={"modalities": ["text"]},
        tools=[],
        input=[
            {
                "id": "msg_probe_user",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"type": "text", "text": "只回复 OK"}],
            }
        ],
        provider_options={},
        metadata={"purpose": "admin_reachability_probe"},
        extensions={},
    )
    result = await execute(request, replace(context, gateway_system_prompt=""))
    return ProviderProbeResult(
        reachable=result.status not in {"failed", "cancelled"},
        status=result.status,
        usage=result.usage,
        provider_response_id=result.provider_response_id,
        error=result.error,
        metadata={"probe_kind": "minimal_inference"},
    )
