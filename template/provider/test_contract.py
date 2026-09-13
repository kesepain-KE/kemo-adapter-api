"""模板自身的契约测试；复制后必须替换为目标厂商的脱敏 Golden Fixture。"""

from __future__ import annotations

import asyncio
import json
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
from .catalog_contract import validate_catalog
from .capabilities import MODEL_CAPABILITIES
from .errors import ExampleErrorMapper
from .provider import ExampleProvider, resolve_api_key


GATEWAY_MODEL = "example-model-name"


def test_full_catalog_matches_manifest_and_runtime() -> None:
    """复制后自动检查所有模型，不只检查 GATEWAY_MODEL 这一条。"""
    directory = Path(__file__).parent
    path = directory / "manifest.json"
    if not path.exists():
        path = directory / "manifest.json.example"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
    validate_catalog(provider.provider_id, provider.models, MODEL_CAPABILITIES, manifest)


@pytest.mark.parametrize("model", sorted(MODEL_CAPABILITIES))
def test_each_registered_model_resolves_to_its_upstream_name(model: str) -> None:
    async def scenario() -> None:
        client = FakeClient()
        provider = ExampleProvider(client)  # type: ignore[arg-type]
        req = request("catalog-mapping").model_copy(update={"model": model})
        await provider.execute(req, context("catalog-mapping"))
        assert client.payloads[-1]["model"] == MODEL_CAPABILITIES[model].metadata["upstream_model"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (
            {
                "api_key": "injected-key",
                "api_keys": [
                    {"key_id": "disabled", "api_key": "disabled-key", "enabled": False},
                    {"key_id": "backup", "api_key": "backup-key", "enabled": True},
                ],
            },
            "injected-key",
        ),
        (
            {
                "api_keys": [
                    {"key_id": "disabled", "api_key": "disabled-key", "enabled": False},
                    {"key_id": "primary", "api_key": "primary-key", "enabled": True},
                ]
            },
            "primary-key",
        ),
        ({"api_key": "legacy-key"}, "legacy-key"),
    ],
)
def test_api_key_resolution_supports_canonical_pool_and_legacy_key(
    settings: dict[str, Any], expected: str
) -> None:
    assert resolve_api_key(settings) == expected
    provider = ExampleProvider.from_settings(settings)
    assert provider._client._api_key == expected  # noqa: SLF001


def test_api_key_resolution_rejects_pool_without_enabled_key() -> None:
    with pytest.raises(ValueError, match="没有启用"):
        resolve_api_key(
            {
                "api_keys": [
                    {"key_id": "disabled", "api_key": "disabled-key", "enabled": False}
                ]
            }
        )


@pytest.mark.parametrize(
    ("pool", "message"),
    [
        ([{"api_key": "missing-id", "enabled": True}], "key_id"),
        (
            [
                {"key_id": "primary", "api_key": "first", "enabled": True},
                {"key_id": "primary", "api_key": "second", "enabled": True},
            ],
            "必须唯一",
        ),
        ([{"key_id": "primary", "api_key": "value", "enabled": "true"}], "布尔"),
        ([{"key_id": "primary", "api_key": "", "enabled": True}], "不能为空"),
    ],
)
def test_api_key_resolution_rejects_noncanonical_pool_entries(
    pool: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_api_key({"api_keys": pool})


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "AUTHENTICATION_ERROR", False),
        (402, "QUOTA_EXCEEDED", False),
        (408, "PROVIDER_TIMEOUT", True),
        (429, "RATE_LIMITED", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (400, "INVALID_REQUEST", False),
    ],
)
def test_error_mapper_keeps_http_retry_boundary_and_redacts_body(
    status: int, code: str, retryable: bool
) -> None:
    error = ExampleErrorMapper().from_http_status(
        status,
        retry_after_ms=12_000,
        provider_request_id="safe-request-id",
    )
    assert error.code == code
    assert error.retryable is retryable
    assert error.provider_status == status
    assert error.retry_after_ms == 12_000
    assert "safe-request-id" in error.details.values()
    assert "response body" not in repr(error).lower()


def test_error_mapper_does_not_treat_ordinary_403_as_key_failure() -> None:
    mapper = ExampleErrorMapper()
    permission_error = mapper.from_http_status(403)
    assert permission_error.code == "PERMISSION_DENIED"
    assert permission_error.retryable is False
    assert permission_error.details.get("key_failure") is not True

    key_error = mapper.from_http_status(403, key_failure=True)
    assert key_error.code == "AUTHENTICATION_ERROR"
    assert key_error.retryable is False
    assert key_error.details.get("key_failure") is True


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
            assert set(reasoning.efforts) <= set(logical)
            effort_map = declaration.extensions["reasoning_effort_map"]
            assert set(effort_map) == set(reasoning.efforts)
            policy = declaration.extensions["reasoning_policy"]
            assert policy["mode"] in {"native", "mapped", "provider_default"}
            assert policy["logical_efforts"] == reasoning.efforts

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


@pytest.mark.parametrize("option", ["unsafe_header", "service_tier", "region"])
def test_unknown_provider_option_is_rejected(option: str) -> None:
    async def scenario() -> None:
        provider = ExampleProvider(FakeClient())  # type: ignore[arg-type]
        with pytest.raises(ProviderException) as captured:
            await provider.execute(
                request("invalid-option", provider_options={option: "example-value"}),
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
