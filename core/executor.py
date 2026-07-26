"""统一执行编排；不包含任何厂商名称或厂商 token 字段。"""

from __future__ import annotations

import hashlib
import json
import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from core.event_assembler import EventAssembler
from core.live_config import LiveConfigManager
from core.models import ErrorObject, KemoRequest, KemoResponse, SSEEvent, Usage
from core.provider_contract import (
    ProviderEvent,
    ProviderEventKind,
    ProviderException,
    ProviderResult,
    RequestContext,
)
from core.registry import ProviderRegistry
from core.runtime_state import ExecutionLease, GatewayRuntimeState
from core.stores import ExecutionRecord, ExecutionStore, InternalStatus


def canonical_request_hash(request: KemoRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class GatewayExecutor:
    def __init__(
        self,
        registry: ProviderRegistry,
        store: ExecutionStore,
        live_config: LiveConfigManager | None = None,
        runtime_state: GatewayRuntimeState | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.live_config = live_config
        self.runtime_state = runtime_state

    async def prepare(self, request: KemoRequest, context: RequestContext) -> tuple[ExecutionRecord, bool]:
        package = self.registry.resolve(request.model)
        record = ExecutionRecord(
            tenant_id=context.tenant_id,
            request_id=request.request_id,
            request_hash=canonical_request_hash(request),
            response_id=context.response_id,
            model=request.model,
            provider_id=package.provider_id,
            subject_id=context.subject_id,
            live_config_revision=context.live_config_revision,
            gateway_system_prompt_hash=(
                hashlib.sha256(context.gateway_system_prompt.encode("utf-8")).hexdigest()
                if context.gateway_system_prompt
                else None
            ),
        )
        return await self.store.create_or_get(record)

    def make_context(self, *, tenant_id: str, subject_id: str, request_id: str) -> RequestContext:
        snapshot = self.live_config.current if self.live_config is not None else None
        return RequestContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            request_id=request_id,
            response_id=f"resp_{uuid4().hex}",
            trace_id=f"trace_{uuid4().hex}",
            gateway_system_prompt=snapshot.gateway_system_prompt if snapshot else "",
            live_config_revision=snapshot.revision if snapshot else "empty",
        )

    def _response_from_result(
        self, request: KemoRequest, record: ExecutionRecord, result: ProviderResult
    ) -> KemoResponse:
        return KemoResponse(
            id=record.response_id,
            request_id=request.request_id,
            status=result.status,  # Provider 契约测试负责保证枚举合法
            model=request.model,
            output=result.output,
            usage=result.usage,
            error=result.error,
            incomplete_details=result.incomplete_details,
            provider_response_id=result.provider_response_id,
            metadata=result.metadata,
            extensions=result.extensions,
        )

    async def execute(
        self,
        request: KemoRequest,
        context: RequestContext,
        *,
        execution_lease: ExecutionLease | None = None,
    ) -> KemoResponse:
        lease = execution_lease
        if lease is None and self.runtime_state is not None:
            lease = await self.runtime_state.admit_execution()
        try:
            record, created = await self.prepare(request, context)
            if not created:
                if record.response is not None:
                    return record.response
                return await self.store.wait_terminal(record)

            package = self.registry.resolve_registered(request.model)
            record.status = InternalStatus.RUNNING
            await self.store.save(record)
            try:
                result = await package.execute(request, context)
            except ProviderException as exc:
                result = ProviderResult(status="failed", error=exc.error)
            except Exception as exc:
                result = ProviderResult(
                    status="failed",
                    error=ErrorObject(
                        type="adapter_contract_error",
                        code="PROVIDER_BAD_RESPONSE",
                        message="Provider adapter failed before producing a valid response.",
                        retryable=True,
                        details={"exception_type": type(exc).__name__},
                    ),
                )
            response = self._response_from_result(request, record, result)
            record.status = InternalStatus(response.status)
            record.response = response
            record.provider_response_id = response.provider_response_id
            await self.store.save(record)
            return response
        finally:
            if lease is not None:
                await lease.release()

    async def stream(
        self,
        request: KemoRequest,
        context: RequestContext,
        *,
        last_event_id: str | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> AsyncIterator[SSEEvent]:
        lease = execution_lease
        if lease is None and self.runtime_state is not None:
            lease = await self.runtime_state.admit_execution()
        lease_owned_by_producer = False
        try:
            record, created = await self.prepare(request, context)
            if created:
                record.status = InternalStatus.RUNNING
                created_event = EventAssembler.created(
                    request_id=request.request_id, response_id=record.response_id
                )
                await self.store.append_event(record, created_event)
                record.producer_task = asyncio.create_task(
                    self._produce_stream(request, context, record, lease),
                    name=f"provider-stream:{record.response_id}",
                )
                lease_owned_by_producer = lease is not None
                await self.store.save(record)

            after_sequence = -1
            if last_event_id is not None:
                matches = [event.sequence for event in record.events if event.event_id == last_event_id]
                if not matches:
                    raise LookupError("Last-Event-ID 不属于该响应或已过期")
                after_sequence = matches[0]
            async for event in self.store.subscribe(record, after_sequence):
                yield event
        finally:
            if lease is not None and not lease_owned_by_producer:
                await lease.release()

    async def _produce_stream(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
        execution_lease: ExecutionLease | None = None,
    ) -> None:
        try:
            await self._produce_stream_inner(request, context, record)
        finally:
            if execution_lease is not None:
                await execution_lease.release()

    async def _produce_stream_inner(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
    ) -> None:
        try:
            package = self.registry.resolve_registered(request.model)
            async for provider_event in package.stream(request, context):
                if record.status in {
                    InternalStatus.CANCELLED,
                    InternalStatus.COMPLETED,
                    InternalStatus.REQUIRES_ACTION,
                    InternalStatus.INCOMPLETE,
                    InternalStatus.FAILED,
                }:
                    return
                if provider_event.provider_response_id:
                    record.provider_response_id = provider_event.provider_response_id
                terminal_response = None
                if provider_event.kind in {
                    ProviderEventKind.COMPLETED,
                    ProviderEventKind.INCOMPLETE,
                    ProviderEventKind.FAILED,
                    ProviderEventKind.CANCELLED,
                }:
                    if provider_event.result is None:
                        raise RuntimeError("Provider 终态缺少完整 result")
                    terminal_response = self._response_from_result(
                        request, record, provider_event.result
                    )
                    record.response = terminal_response
                    record.status = InternalStatus(terminal_response.status)
                    record.provider_response_id = terminal_response.provider_response_id

                event = EventAssembler.assemble(
                    provider_event,
                    request_id=request.request_id,
                    response_id=record.response_id,
                    sequence=len(record.events),
                    terminal_response=terminal_response,
                )
                await self.store.append_event(record, event)
                if terminal_response is not None:
                    await self.store.save(record)
                    return
        except asyncio.CancelledError:
            return
        except ProviderException as exc:
            result = ProviderResult(status="failed", error=exc.error)
            response = self._response_from_result(request, record, result)
            record.response = response
            record.status = InternalStatus.FAILED
            failed = EventAssembler.assemble(
                ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=exc.error),
                request_id=request.request_id,
                response_id=record.response_id,
                sequence=len(record.events),
                terminal_response=response,
            )
            await self.store.append_event(record, failed)
            await self.store.save(record)
            return
        except Exception as exc:
            error = ErrorObject(
                type="adapter_contract_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider stream violated the adapter contract.",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            )
            result = ProviderResult(status="failed", error=error)
            response = self._response_from_result(request, record, result)
            record.response = response
            record.status = InternalStatus.FAILED
            failed = EventAssembler.assemble(
                ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=error),
                request_id=request.request_id,
                response_id=record.response_id,
                sequence=len(record.events),
                terminal_response=response,
            )
            await self.store.append_event(record, failed)
            await self.store.save(record)
            return

        if record.response is None:
            error = ErrorObject(
                type="gateway_protocol_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider 流在统一终态之前结束。",
                retryable=True,
            )
            result = ProviderResult(status="failed", usage=Usage(), error=error)
            response = self._response_from_result(request, record, result)
            record.response = response
            record.status = InternalStatus.FAILED
            failed = EventAssembler.assemble(
                ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=error),
                request_id=request.request_id,
                response_id=record.response_id,
                sequence=len(record.events),
                terminal_response=response,
            )
            await self.store.append_event(record, failed)
            await self.store.save(record)

    async def get(self, tenant_id: str, response_id: str) -> KemoResponse | None:
        record = await self.store.get_by_response_id(tenant_id, response_id)
        if record is None:
            return None
        if record.response is not None:
            return record.response
        return KemoResponse(
            id=record.response_id,
            request_id=record.request_id,
            status="incomplete",
            model=record.model,
            incomplete_details={"reason": "running"},
        )

    async def cancel(
        self, *, tenant_id: str, subject_id: str, response_id: str
    ) -> KemoResponse | None:
        record = await self.store.get_by_response_id(tenant_id, response_id)
        if record is None:
            return None
        if record.response is not None:
            return record.response

        package = self.registry.resolve_registered(record.model)
        context = RequestContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            request_id=record.request_id,
            response_id=record.response_id,
            trace_id=f"trace_{uuid4().hex}",
            gateway_system_prompt=(
                self.live_config.current.gateway_system_prompt if self.live_config else ""
            ),
            live_config_revision=(
                self.live_config.current.revision if self.live_config else "empty"
            ),
        )
        await package.cancel(record.provider_response_id, context)
        response = KemoResponse(
            id=record.response_id,
            request_id=record.request_id,
            status="cancelled",
            model=record.model,
        )
        record.status = InternalStatus.CANCELLED
        record.response = response
        cancelled = EventAssembler.assemble(
            ProviderEvent(
                kind=ProviderEventKind.CANCELLED,
                result=ProviderResult(status="cancelled"),
            ),
            request_id=record.request_id,
            response_id=record.response_id,
            sequence=len(record.events),
            terminal_response=response,
        )
        await self.store.append_event(record, cancelled)
        if record.producer_task is not None and not record.producer_task.done():
            record.producer_task.cancel()
        await self.store.save(record)
        return response
