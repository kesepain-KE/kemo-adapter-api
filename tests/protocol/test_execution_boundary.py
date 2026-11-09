from __future__ import annotations

from tests.support.llm import FakeProvider, request

import asyncio
from contextlib import AsyncExitStack
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
    ProviderResult,
    RequestContext,
)
from tests.support.gateway import fixture_registry
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


async def executor(root, resources) -> GatewayExecutor:
    registry = await resources.enter_async_context(fixture_registry(root, FakeProvider()))
    return GatewayExecutor(registry, InMemoryExecutionStore())


def test_non_stream_usage_is_preserved_without_gateway_reinterpretation(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            gateway = await executor(tmp_path, resources)
            context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
            response = await gateway.execute(request(stream=False), context)

            assert response.usage.input_tokens == 10
            assert response.usage.output_tokens == 4
            assert response.usage.reasoning_tokens == 3
            assert response.usage.total_tokens == 14
            assert response.usage.measurement.mode == "provider"

    asyncio.run(scenario())


def test_core_owns_sse_sequence_ids_and_terminal_envelope(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            gateway = await executor(tmp_path, resources)
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


def test_stream_resume_reuses_stored_event_ids(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            gateway = await executor(tmp_path, resources)
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


def test_same_request_id_with_different_body_conflicts(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            gateway = await executor(tmp_path, resources)
            first_context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
            await gateway.execute(request(stream=False), first_context)
            second_context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")

            with pytest.raises(IdempotencyConflict):
                await gateway.execute(request(stream=False, system_prompt="different"), second_context)

    asyncio.run(scenario())


def test_broken_adapter_becomes_sanitized_terminal_failure(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            registry = await resources.enter_async_context(fixture_registry(tmp_path, BrokenStreamProvider()))
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


@pytest.mark.parametrize("invalid_delta", [True, False], ids=["invalid-delta", "missing-terminal"])
def test_rejected_stream_closes_provider_and_records_failed_attempt(tmp_path, invalid_delta) -> None:
    class InvalidStreamProvider(FakeProvider):
        closed = False

        async def _stream(self, request, context):
            try:
                yield ProviderEvent(
                    kind=ProviderEventKind.REASONING_CONTENT_DELTA,
                    item_id="rs_1",
                    delta="think",
                    item={"type": "reasoning"} if invalid_delta else None,
                )
                if invalid_delta:
                    yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=self.result())
            finally:
                self.closed = True

    async def scenario() -> None:
        provider = InvalidStreamProvider()
        async with fixture_registry(tmp_path, provider) as registry:
            store = InMemoryExecutionStore()
            gateway = GatewayExecutor(registry, store)
            context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
            events = [event async for event in gateway.stream(request(stream=True), context)]
            assert events[-1].type == "response.failed"
            assert events[-1].response.error.code == "PROVIDER_BAD_RESPONSE"
            record = await store.get_by_response_id("t1", events[-1].response_id)
            await record.producer_task
            assert record.status.value == "failed"
            assert provider.closed
            attempts = registry.store.attempts()
            assert len(attempts) == 1
            assert attempts[0]["status"] == "failed"
            assert attempts[0]["error_code"] == "PROVIDER_BAD_RESPONSE"

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_during", ["provider", "consumer"])
def test_stream_cancellation_closes_provider_and_preserves_cancelled_attempt(tmp_path, cancel_during) -> None:
    class WaitingProvider(FakeProvider):
        def __init__(self):
            self.waiting = asyncio.Event()
            self.closed = False

        async def _stream(self, request, context):
            try:
                yield ProviderEvent(
                    kind=ProviderEventKind.TEXT_DELTA, item_id="msg_1", content_index=0, delta="ok",
                )
                self.waiting.set()
                await asyncio.Event().wait()
            finally:
                self.closed = True

    async def scenario() -> None:
        provider = WaitingProvider()

        class WaitingStore(InMemoryExecutionStore):
            async def append_event(self, record, event):
                await super().append_event(record, event)
                if cancel_during == "consumer" and event.type == "output_text.delta":
                    provider.waiting.set()
                    await asyncio.Event().wait()

        async with fixture_registry(tmp_path, provider) as registry:
            gateway = GatewayExecutor(registry, WaitingStore())
            context = gateway.make_context(tenant_id="t1", subject_id="u1", request_id="req_1")
            prepared = await gateway.prepare_stream(request(stream=True), context)
            await asyncio.wait_for(provider.waiting.wait(), timeout=1)
            response = await gateway.cancel(
                tenant_id="t1", subject_id="u1", response_id=prepared.record.response_id,
            )
            with pytest.raises(asyncio.CancelledError):
                await prepared.record.producer_task
            events = [event async for event in gateway.iter_prepared_stream(prepared)]
            assert response.status == prepared.record.status.value == "cancelled"
            assert events[-1].type == "response.cancelled"
            assert provider.closed
            attempts = registry.store.attempts()
            assert len(attempts) == 1 and attempts[0]["status"] == "cancelled"
            assert attempts[0]["error_code"] is None

    asyncio.run(scenario())


def test_standalone_error_is_terminal_without_response_failed_duplicate(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            registry = await resources.enter_async_context(fixture_registry(tmp_path, StandaloneErrorProvider()))
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
        async with AsyncExitStack() as resources:
            store = SQLiteExecutionStore(tmp_path, cleanup_startup_delay_seconds=60)
            await store.initialize()
            registry = await resources.enter_async_context(fixture_registry(tmp_path, StandaloneErrorProvider()))
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
            replay_registry = await resources.enter_async_context(fixture_registry(tmp_path / "replay"))
            replay_gateway = GatewayExecutor(replay_registry, replay_store)
            replay = await replay_gateway.get("t1", response_id)
            assert replay is not None
            assert replay.status == "failed"
            assert replay.error is not None
            assert replay.error.code == "PROVIDER_TIMEOUT"
            await replay_store.close()

    asyncio.run(scenario())


def test_core_timeout_normalizes_non_stream_and_stream_failures(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            registry = await resources.enter_async_context(fixture_registry(tmp_path, SlowProvider()))
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


def test_non_stream_producer_survives_caller_cancellation_for_idempotent_replay(tmp_path) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            registry = await resources.enter_async_context(fixture_registry(tmp_path, SlowProvider()))
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
