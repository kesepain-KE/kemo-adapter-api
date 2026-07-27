"""模板自身的契约测试；复制后必须替换为目标厂商的脱敏 Golden Fixture。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from core.models import KemoRequest
from core.provider_contract import ProviderEventKind, ProviderException, RequestContext

from .provider import ExampleProvider


GATEWAY_MODEL = "example-model-name"


class FakeClient:
    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    async def create(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.payloads.append(payload)
        return {
            "id": "sanitized-provider-response-id",
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
            "usage": {
                "vendor_input_tokens": 8,
                "vendor_output_tokens": 1,
                "vendor_total_tokens": 9,
            },
        }

    async def stream(
        self, payload: Mapping[str, Any]
    ) -> AsyncIterator[Mapping[str, Any]]:
        self.payloads.append(payload)
        yield {"id": "sanitized-stream-id", "vendor_type": "text_delta", "delta": "OK"}
        yield {
            "id": "sanitized-stream-id",
            "vendor_type": "completed",
            "usage": {
                "vendor_input_tokens": 8,
                "vendor_output_tokens": 1,
                "vendor_total_tokens": 9,
            },
        }

    async def cancel(self, provider_response_id: str) -> None:
        del provider_response_id

    async def close(self) -> None:
        return None


def context(request_id: str) -> RequestContext:
    return RequestContext(
        tenant_id="test-tenant",
        subject_id="test-subject",
        request_id=request_id,
        response_id=f"resp-{request_id}",
        trace_id=f"trace-{request_id}",
    )


def request(
    request_id: str,
    *,
    stream: bool = False,
    provider_options: dict[str, Any] | None = None,
) -> KemoRequest:
    return KemoRequest(
        protocol_version="1.0",
        request_id=request_id,
        attempt=1,
        model=GATEWAY_MODEL,
        stream=stream,
        system_prompt="",
        generation={"max_output_tokens": 64},
        output={"modalities": ["text"]},
        tools=[],
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": "只回复 OK"}],
            }
        ],
        provider_options=provider_options or {},
        metadata={},
        extensions={},
    )


def test_models_capabilities_and_prefix_are_consistent() -> None:
    async def scenario() -> None:
        provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
        assert provider.models == {GATEWAY_MODEL}
        assert all(model.startswith(f"{provider.provider_id}-") for model in provider.models)
        declaration = await provider.capabilities(GATEWAY_MODEL)
        assert declaration.model == GATEWAY_MODEL
        assert declaration.task == "llm"

    asyncio.run(scenario())


def test_probe_is_real_minimal_inference() -> None:
    async def scenario() -> None:
        client = FakeClient()
        provider = ExampleProvider(client)  # type: ignore[arg-type]
        result = await provider.probe(GATEWAY_MODEL, context("probe-1"))
        assert result.reachable is True
        assert result.status == "completed"
        assert result.usage.total_tokens == 9
        assert client.payloads

    asyncio.run(scenario())


def test_unknown_provider_option_is_rejected() -> None:
    async def scenario() -> None:
        provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
        with pytest.raises(ProviderException) as captured:
            await provider.execute(
                request("invalid-option", provider_options={"unsafe_header": "secret"}),
                context("invalid-option"),
            )
        assert captured.value.error.code == "VALIDATION_ERROR"

    asyncio.run(scenario())


def test_stream_has_one_terminal_result_and_provider_has_no_sse_envelope() -> None:
    async def scenario() -> None:
        provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
        events = [
            event
            async for event in provider.stream(
                request("stream-1", stream=True),
                context("stream-1"),
            )
        ]
        terminals = [
            event for event in events
            if event.kind in {
                ProviderEventKind.COMPLETED,
                ProviderEventKind.INCOMPLETE,
                ProviderEventKind.FAILED,
                ProviderEventKind.CANCELLED,
            }
        ]
        assert len(terminals) == 1
        assert terminals[0].result is not None
        assert terminals[0].result.status == "completed"
        assert all(not hasattr(event, "sequence") for event in events)

    asyncio.run(scenario())
