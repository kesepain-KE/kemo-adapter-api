from __future__ import annotations

import asyncio
from types import SimpleNamespace
from collections.abc import AsyncIterator

import pytest

from core.executor import GatewayExecutor
from core.models import (
    KemoRequest,
    ModelCapabilities,
    StageUsage,
    Usage,
    UsageMeasurement,
)
from core.provider_contract import (
    ProviderEvent,
    ProviderEventKind,
    ProviderPackage,
    ProviderResult,
    RequestContext,
)
from core.registry import ProviderRegistry
from core.stores import IdempotencyConflict, InMemoryExecutionStore
from core.usage import aggregate_stages


class FakeProvider(ProviderPackage):
    provider_id = "fake"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"fake-model"})

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            model=model,
            input_modalities=["text"],
            output_modalities=["text"],
            streaming=True,
        )

    def result(self) -> ProviderResult:
        return ProviderResult(
            status="completed",
            output=[
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "status": "completed",
                    "content": [{"type": "text", "text": "ok"}],
                    "metadata": {},
                    "extensions": {},
                }
            ],
            usage=Usage(
                input_tokens=10,
                output_tokens=4,
                reasoning_tokens=3,
                total_tokens=14,
                measurement=UsageMeasurement(
                    mode="provider",
                    exact=True,
                    exact_fields=["input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"],
                ),
            ),
            provider_response_id="vendor_1",
        )

    async def execute(self, request: KemoRequest, context: RequestContext) -> ProviderResult:
        return self.result()

    async def _stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA,
            item_id="msg_1",
            content_index=0,
            delta="ok",
            provider_response_id="vendor_1",
        )
        yield ProviderEvent(kind=ProviderEventKind.USAGE, usage=self.result().usage)
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=self.result())

    def stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        return self._stream(request, context)


class BrokenStreamProvider(FakeProvider):
    provider_id = "broken"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"broken-model"})

    async def _stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        if False:
            yield ProviderEvent(kind=ProviderEventKind.TEXT_DELTA)
        raise ValueError("sensitive vendor body must not escape")


class SlashNamedProvider(FakeProvider):
    @property
    def models(self) -> frozenset[str]:
        return frozenset({"fake/model"})


class HyphenatedProvider(FakeProvider):
    provider_id = "custom-provider"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"custom-provider-upstream-model-v2"})


def request(*, stream: bool, system_prompt: str = "system") -> KemoRequest:
    return KemoRequest(
        protocol_version="1.0",
        request_id="req_1",
        attempt=1,
        model="fake-model",
        stream=stream,
        system_prompt=system_prompt,
        generation={},
        output={"modalities": ["text"]},
        tools=[],
        input=[],
        provider_options={},
        metadata={},
        extensions={},
    )


def executor() -> GatewayExecutor:
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    return GatewayExecutor(registry, InMemoryExecutionStore())


def test_registry_uses_canonical_provider_prefix_without_parsing_hyphens() -> None:
    registry = ProviderRegistry()
    provider = HyphenatedProvider()
    registry.register(provider)

    assert registry.resolve("custom-provider-upstream-model-v2") is provider
    with pytest.raises(LookupError, match="没有注册模型"):
        registry.resolve("custom/provider-upstream-model-v2")


def test_registry_rejects_deprecated_slash_model_names() -> None:
    with pytest.raises(ValueError, match="fake-"):
        ProviderRegistry().register(SlashNamedProvider())


def test_discovery_rejects_provider_id_that_differs_from_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_info = SimpleNamespace(name="providers.folder_name", ispkg=True)
    package = FakeProvider()
    monkeypatch.setattr("core.registry.pkgutil.iter_modules", lambda *_: [module_info])
    monkeypatch.setattr(
        "core.registry.importlib.import_module",
        lambda *_: SimpleNamespace(create_provider=lambda settings: package),
    )

    with pytest.raises(ValueError, match="Provider ID 必须与目录名一致"):
        ProviderRegistry().discover({})


def test_non_stream_usage_is_preserved_without_gateway_reinterpretation() -> None:
    async def scenario() -> None:
        gateway = executor()
        context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
        response = await gateway.execute(request(stream=False), context)

        assert response.usage.input_tokens == 10
        assert response.usage.output_tokens == 4
        assert response.usage.reasoning_tokens == 3
        assert response.usage.total_tokens == 14
        assert response.usage.measurement.mode == "provider"

    asyncio.run(scenario())


def test_core_owns_sse_sequence_ids_and_terminal_envelope() -> None:
    async def scenario() -> None:
        gateway = executor()
        context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
        events = [event async for event in gateway.stream(request(stream=True), context)]

        assert [event.sequence for event in events] == [0, 1, 2, 3]
        assert [event.type for event in events] == [
            "response.created",
            "output_text.delta",
            "usage.updated",
            "response.completed",
        ]
        assert len({event.event_id for event in events}) == len(events)
        assert events[-1].response is not None
        assert events[-1].response.provider_response_id == "vendor_1"

    asyncio.run(scenario())


def test_stream_resume_reuses_stored_event_ids() -> None:
    async def scenario() -> None:
        gateway = executor()
        first_context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
        first = [event async for event in gateway.stream(request(stream=True), first_context)]

        replay_context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
        replay = [
            event
            async for event in gateway.stream(
                request(stream=True), replay_context, last_event_id=first[1].event_id
            )
        ]
        assert [event.event_id for event in replay] == [event.event_id for event in first[2:]]

    asyncio.run(scenario())


def test_same_request_id_with_different_body_conflicts() -> None:
    async def scenario() -> None:
        gateway = executor()
        first_context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
        await gateway.execute(request(stream=False), first_context)
        second_context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")

        with pytest.raises(IdempotencyConflict):
            await gateway.execute(request(stream=False, system_prompt="different"), second_context)

    asyncio.run(scenario())


def test_broken_adapter_becomes_sanitized_terminal_failure() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        registry.register(BrokenStreamProvider())
        gateway = GatewayExecutor(registry, InMemoryExecutionStore())
        broken_request = request(stream=True).model_copy(update={"model": "broken-model"})
        context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
        events = [event async for event in gateway.stream(broken_request, context)]

        assert [event.type for event in events] == ["response.created", "response.failed"]
        assert events[-1].response is not None
        assert events[-1].response.error is not None
        assert events[-1].response.error.code == "PROVIDER_BAD_RESPONSE"
        assert "sensitive vendor body" not in events[-1].response.error.message

    asyncio.run(scenario())


def test_stage_aggregation_uses_provider_totals_without_double_counting_reasoning() -> None:
    usage = aggregate_stages(
        [
            StageUsage(
                stage="main_inference",
                provider="fake",
                model="fake-model",
                input_tokens=10,
                output_tokens=8,
                reasoning_tokens=6,
                total_tokens=18,
                media={"input_audio_seconds": 2.5},
                measurement=UsageMeasurement(mode="provider", exact=True),
            ),
            StageUsage(
                stage="audio_transcription",
                provider="fake",
                model="fake-asr",
                output_tokens=2,
                total_tokens=2,
                media={"input_audio_seconds": 3.5},
                measurement=UsageMeasurement(mode="provider", exact=True),
            ),
        ]
    )

    assert usage.output_tokens == 10
    assert usage.reasoning_tokens == 6
    assert usage.total_tokens == 20
    assert usage.media["input_audio_seconds"] == 6.0
