from __future__ import annotations

from tests.support.llm import FakeProvider, request

import asyncio
from types import SimpleNamespace
from collections.abc import AsyncIterator

import pytest

from core.executor import GatewayExecutor
from core.models import (
    ErrorObject,
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
from core.registry import ProviderRegistry, _factory_settings
from core.provider_keys import normalize_provider_keys
from core.stores import IdempotencyConflict, InMemoryExecutionStore, SQLiteExecutionStore
from core.usage import aggregate_stages


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


class StandaloneErrorProvider(FakeProvider):
    """Provider that reports a stream-level Kemo 2.0 error without a response."""

    provider_id = "standalone-error"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"standalone-error-model"})

    async def _stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        del request, context
        yield ProviderEvent(
            kind=ProviderEventKind.ERROR,
            error=ErrorObject(
                type="provider_error",
                code="PROVIDER_TIMEOUT",
                message="upstream timed out before producing a response",
                retryable=True,
                retry_scope="same_sequence",
                retry_after_ms=250,
            ),
        )

    def stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        return self._stream(request, context)


class SlowProvider(FakeProvider):
    async def execute(
        self, request: KemoRequest, context: RequestContext
    ) -> ProviderResult:
        await asyncio.sleep(0.1)
        return self.result()

    async def _stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(0.1)
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=self.result())


class SlashNamedProvider(FakeProvider):
    @property
    def models(self) -> frozenset[str]:
        return frozenset({"fake/model"})


class HyphenatedProvider(FakeProvider):
    provider_id = "custom-provider"

    @property
    def models(self) -> frozenset[str]:
        return frozenset({"custom-provider-upstream-model-v2"})


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


def test_discovery_injects_canonical_single_key_and_exposes_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_info = SimpleNamespace(name="providers.fake", ispkg=True)
    received: list[dict[str, object]] = []

    def factory(settings: object) -> FakeProvider:
        assert isinstance(settings, dict)
        received.append(settings)
        return FakeProvider()

    monkeypatch.setattr("core.registry.pkgutil.iter_modules", lambda *_: [module_info])
    monkeypatch.setattr(
        "core.registry.importlib.import_module",
        lambda *_: SimpleNamespace(create_provider=factory),
    )

    registry = ProviderRegistry()
    registry.discover(
        {
            "fake": {
                "base_url": "https://provider.invalid",
                "api_keys": [
                    {
                        "key_id": "primary",
                        "api_key": "canonical-provider-secret",
                        "enabled": True,
                    }
                ],
            }
        }
    )

    assert len(received) == 1
    assert received[0]["api_key"] == "canonical-provider-secret"
    package = registry.providers["fake"]
    statuses = package.key_statuses()
    assert [item["key_id"] for item in statuses] == ["primary"]
    assert "canonical-provider-secret" not in repr(statuses)


def test_discovery_rejects_all_disabled_pool_instead_of_using_legacy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_info = SimpleNamespace(name="providers.fake", ispkg=True)

    def factory(settings: object) -> FakeProvider:
        assert isinstance(settings, dict)
        assert settings.get("api_key") is None
        return FakeProvider()

    monkeypatch.setattr("core.registry.pkgutil.iter_modules", lambda *_: [module_info])
    monkeypatch.setattr(
        "core.registry.importlib.import_module",
        lambda *_: SimpleNamespace(create_provider=factory),
    )

    with pytest.raises(ValueError, match="没有启用的 api_keys"):
        ProviderRegistry().discover(
            {
                "fake": {
                    "api_key": "stale-legacy-secret",
                    "api_keys": [
                        {
                            "key_id": "primary",
                            "api_key": "disabled-secret",
                            "enabled": False,
                        }
                    ],
                }
            }
        )


def test_discovery_rejects_explicit_empty_pool_instead_of_using_legacy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_info = SimpleNamespace(name="providers.fake", ispkg=True)
    monkeypatch.setattr("core.registry.pkgutil.iter_modules", lambda *_: [module_info])
    monkeypatch.setattr(
        "core.registry.importlib.import_module",
        lambda *_: SimpleNamespace(create_provider=lambda settings: FakeProvider()),
    )

    with pytest.raises(ValueError, match="api_keys 不能为空"):
        ProviderRegistry().discover(
            {"fake": {"api_keys": [], "api_key": "stale-legacy-secret"}}
        )


def test_factory_settings_does_not_fall_back_to_disabled_legacy_key() -> None:
    settings = {
        "api_key": "stale-legacy-secret",
        "api_key_id": "legacy",
        "api_keys": [
            {"key_id": "disabled", "api_key": "new-secret", "enabled": False}
        ],
    }
    pool = normalize_provider_keys(settings)
    prepared = _factory_settings(settings, pool)

    assert prepared["api_keys"] == settings["api_keys"]
    assert "api_key" not in prepared
    assert "api_key_id" not in prepared


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

        assert [event.sequence for event in events] == [0, 1, 2, 3, 4, 5]
        assert [event.type for event in events] == [
            "response.created",
            "output_item.added",
            "output_text.delta",
            "output_text.done",
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


def test_standalone_error_is_terminal_without_response_failed_duplicate() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        registry.register(StandaloneErrorProvider())
        store = InMemoryExecutionStore()
        gateway = GatewayExecutor(registry, store)
        standalone_request = request(stream=True).model_copy(
            update={
                "model": "standalone-error-model",
                "request_id": "req_standalone_error",
            }
        )
        context = gateway.make_context(
            tenant_id="t1",
            subject_id="u1",
            request_id=standalone_request.request_id,
        )

        events = [event async for event in gateway.stream(standalone_request, context)]

        assert [event.type for event in events] == ["response.created", "error"]
        terminal = events[-1]
        assert terminal.response is None
        assert terminal.error is not None
        assert terminal.error.code == "PROVIDER_TIMEOUT"
        assert terminal.error.retryable is True
        assert all(event.type != "response.failed" for event in events)

        stored = await store.get_by_response_id("t1", terminal.response_id)
        assert stored is not None
        assert stored.status.value == "failed"
        assert stored.response is not None
        assert stored.response.status == "failed"
        assert stored.response.error is not None
        assert stored.response.error.code == "PROVIDER_TIMEOUT"

        replay = await gateway.get("t1", terminal.response_id)
        assert replay is not None
        assert replay.status == "failed"
        assert replay.error is not None
        assert replay.error.code == "PROVIDER_TIMEOUT"

    asyncio.run(scenario())


def test_standalone_error_persists_failed_snapshot_in_sqlite(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteExecutionStore(tmp_path, cleanup_startup_delay_seconds=60)
        await store.initialize()
        registry = ProviderRegistry()
        registry.register(StandaloneErrorProvider())
        gateway = GatewayExecutor(registry, store)
        standalone_request = request(stream=True).model_copy(
            update={
                "model": "standalone-error-model",
                "request_id": "req_standalone_error_sqlite",
            }
        )
        context = gateway.make_context(
            tenant_id="t1",
            subject_id="u1",
            request_id=standalone_request.request_id,
        )

        events = [event async for event in gateway.stream(standalone_request, context)]
        response_id = events[-1].response_id
        assert [event.type for event in events] == ["response.created", "error"]

        await store.close()

        replay_store = SQLiteExecutionStore(tmp_path, cleanup_startup_delay_seconds=60)
        await replay_store.initialize()
        replay_gateway = GatewayExecutor(ProviderRegistry(), replay_store)
        replay = await replay_gateway.get("t1", response_id)
        assert replay is not None
        assert replay.status == "failed"
        assert replay.error is not None
        assert replay.error.code == "PROVIDER_TIMEOUT"
        await replay_store.close()

    asyncio.run(scenario())


def test_core_timeout_normalizes_non_stream_and_stream_failures() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        registry.register(SlowProvider())
        gateway = GatewayExecutor(
            registry,
            InMemoryExecutionStore(),
            execution_timeout_seconds=0.02,
        )
        non_stream = request(stream=False)
        response = await gateway.execute(
            non_stream,
            gateway.make_context(
                tenant_id="t1", subject_id="u1", request_id=non_stream.request_id
            ),
        )
        assert response.status == "failed"
        assert response.error is not None
        assert response.error.code == "GATEWAY_TIMEOUT"
        assert response.error.retryable is True

        stream_request = request(stream=True).model_copy(
            update={"request_id": "req_stream_timeout"}
        )
        events = [
            event
            async for event in gateway.stream(
                stream_request,
                gateway.make_context(
                    tenant_id="t1",
                    subject_id="u1",
                    request_id=stream_request.request_id,
                ),
            )
        ]
        assert [event.type for event in events] == [
            "response.created",
            "response.failed",
        ]
        assert events[-1].response is not None
        assert events[-1].response.error is not None
        assert events[-1].response.error.code == "GATEWAY_TIMEOUT"

    asyncio.run(scenario())


def test_non_stream_producer_survives_caller_cancellation_for_idempotent_replay() -> None:
    async def scenario() -> None:
        registry = ProviderRegistry()
        registry.register(SlowProvider())
        gateway = GatewayExecutor(
            registry,
            InMemoryExecutionStore(),
            execution_timeout_seconds=1,
        )
        call = request(stream=False)
        context = gateway.make_context(
            tenant_id="t1", subject_id="u1", request_id=call.request_id
        )
        caller = asyncio.create_task(gateway.execute(call, context))
        await asyncio.sleep(0.01)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.sleep(0.12)

        replay = await gateway.execute(
            call,
            gateway.make_context(
                tenant_id="t1", subject_id="u1", request_id=call.request_id
            ),
        )
        assert replay.status == "completed"
        assert replay.output[0].type == "message"

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
