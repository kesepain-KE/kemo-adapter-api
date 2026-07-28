"""模板自身的契约测试；复制后必须替换为目标厂商的脱敏 Golden Fixture。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.models import AssetDescriptor, KemoRequest
from core.provider_contract import (
    ProviderEventKind,
    ProviderException,
    RequestContext,
    ResolvedAsset,
)

from .media import parse_media_block, store_output_media
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
                "id": "msg_test_user",
                "type": "message",
                "role": "user",
                "status": "completed",
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


def test_reasoning_declaration_uses_only_verified_kemo_efforts() -> None:
    async def scenario() -> None:
        provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
        declaration = await provider.capabilities(GATEWAY_MODEL)
        reasoning = declaration.reasoning
        logical = ["minimal", "low", "medium", "high", "max"]

        assert len(reasoning.efforts) == len(set(reasoning.efforts))
        if not reasoning.supported:
            assert reasoning.efforts == []
            assert reasoning.summary is False
            assert reasoning.persisted_state is False
        else:
            assert reasoning.efforts == logical
            effort_map = declaration.extensions["reasoning_effort_map"]
            assert list(effort_map) == logical
            policy = declaration.extensions["reasoning_policy"]
            assert policy["mode"] in {"native", "mapped", "provider_default"}
            assert policy["logical_efforts"] == logical

    asyncio.run(scenario())


def test_legacy_reasoning_effort_cannot_bypass_model_declaration() -> None:
    async def scenario() -> None:
        provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
        with pytest.raises(ProviderException) as captured:
            await provider.execute(
                request(
                    "unsupported-reasoning",
                    provider_options={"reasoning_effort": "high"},
                ),
                context("unsupported-reasoning"),
            )
        assert captured.value.error.code == "VALIDATION_ERROR"

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


def test_asset_input_and_output_helpers_keep_paths_inside_provider(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    input_path = tmp_path / "input.png"
    input_path.write_bytes(b"test")
    input_descriptor = AssetDescriptor(
        id="asset_input_test",
        status="ready",
        purpose="input",
        filename="input.png",
        mime_type="image/png",
        size=4,
        checksum_sha256="0" * 64,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    class FakeAssets:
        def resolve(self, asset_id: str) -> ResolvedAsset:
            assert asset_id == input_descriptor.id
            return ResolvedAsset(descriptor=input_descriptor, path=input_path)

        async def store_output(self, **kwargs: Any) -> AssetDescriptor:
            assert kwargs["mime_type"] == "image/png"
            return input_descriptor.model_copy(
                update={"id": "asset_output_test", "purpose": "output"}
            )

    assets = FakeAssets()
    parsed = parse_media_block(
        {"type": "image", "asset_id": input_descriptor.id},
        expected_type="image",
        location="input[0].content[0]",
        assets=assets,  # type: ignore[arg-type]
    )
    assert parsed.kind == "asset"
    assert parsed.asset_path == input_path

    async def scenario() -> None:
        request_context = RequestContext(
            tenant_id="test-tenant",
            subject_id="test-subject",
            request_id="media-output",
            response_id="resp-media-output",
            trace_id="trace-media-output",
            assets=assets,  # type: ignore[arg-type]
        )
        block, descriptor = await store_output_media(
            request_context,
            media_type="image",
            filename="output.png",
            mime_type="image/png",
            content=b"test",
        )
        assert block == {
            "type": "image",
            "asset_id": "asset_output_test",
            "mime_type": "image/png",
            "checksum_sha256": "0" * 64,
        }
        assert descriptor.purpose == "output"

    asyncio.run(scenario())
