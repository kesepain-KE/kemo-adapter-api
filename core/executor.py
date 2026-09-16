"""统一执行编排；不包含任何厂商名称或厂商 token 字段。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from uuid import uuid4

from core.assets import AssetStore
from core.capability_validation import (
    validate_llm_request_capabilities,
    validate_media_url_networks,
)
from core.event_assembler import EventAssembler
from core.live_config import LiveConfigManager
from core.models import (
    AudioContent,
    ErrorObject,
    FileContent,
    ImageContent,
    KemoRequest,
    KemoResponse,
    MessageItem,
    SSEEvent,
    TextContent,
    ToolCallItem,
    Usage,
    VideoContent,
)
from core.provider_contract import (
    ProviderEvent,
    ProviderEventKind,
    ProviderException,
    ProviderPackage,
    ProviderResult,
    RequestContext,
)
from core.registry import ProviderRegistry
from core.runtime_state import ExecutionLease, GatewayRuntimeState
from core.stores import ExecutionRecord, ExecutionStore, InternalStatus
from core.tool_arguments import validate_tool_call_output
from storage.statistics import InvocationHandle, StatisticsStore


logger = logging.getLogger(__name__)


def canonical_request_hash(request: KemoRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(slots=True)
class PreparedStream:
    record: ExecutionRecord
    after_sequence: int
    execution_lease: ExecutionLease | None
    lease_owned_by_producer: bool


class StreamResumeError(LookupError):
    pass


class GatewayExecutor:
    def __init__(
        self,
        registry: ProviderRegistry,
        store: ExecutionStore,
        live_config: LiveConfigManager | None = None,
        runtime_state: GatewayRuntimeState | None = None,
        statistics: StatisticsStore | None = None,
        assets: AssetStore | None = None,
        execution_timeout_seconds: float = 900.0,
    ) -> None:
        self.registry = registry
        self.store = store
        self.live_config = live_config
        self.runtime_state = runtime_state
        self.statistics = statistics
        self.assets = assets
        self.execution_timeout_seconds = max(0.01, execution_timeout_seconds)

    async def prepare(
        self, request: KemoRequest, context: RequestContext
    ) -> tuple[ExecutionRecord, bool, ProviderPackage | None]:
        package = self.registry.acquire_active(request.model)
        try:
            await self._validate_package_request(request, context, package)
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
            resolved, created = await self.store.create_or_get(record)
            if created:
                # The producer task owns this reference until its Provider
                # call and all response normalization have completed.
                return resolved, True, package
            await self.registry.release_registered(package)
            return resolved, False, None
        except Exception:
            await self.registry.release_registered(package)
            raise

    async def validate_request(
        self, request: KemoRequest, context: RequestContext
    ) -> ProviderPackage:
        """在创建响应或发送 SSE Header 前完成无副作用的协议预检。"""
        package = self.registry.resolve(request.model)
        await self._validate_package_request(request, context, package)
        return package

    async def _validate_package_request(
        self,
        request: KemoRequest,
        context: RequestContext,
        package: ProviderPackage,
    ) -> None:
        capabilities = await package.capabilities(request.model)
        validate_llm_request_capabilities(
            request,
            capabilities,
            asset_access=context.assets,
        )
        await validate_media_url_networks(request)

    def make_context(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        request_id: str,
        gateway_key_id: str | None = None,
    ) -> RequestContext:
        snapshot = self.live_config.current if self.live_config is not None else None
        return RequestContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            request_id=request_id,
            response_id=f"resp_{uuid4().hex}",
            trace_id=f"trace_{uuid4().hex}",
            gateway_system_prompt=snapshot.gateway_system_prompt if snapshot else "",
            live_config_revision=snapshot.revision if snapshot else "empty",
            gateway_key_id=gateway_key_id,
            assets=(
                self.assets.bind(tenant_id, subject_id)
                if self.assets is not None
                else None
            ),
        )

    async def _begin_statistics(
        self, request: KemoRequest, context: RequestContext, record: ExecutionRecord
    ) -> InvocationHandle | None:
        if self.statistics is None:
            return None
        return await self.statistics.begin_invocation(
            task="llm",
            provider_id=record.provider_id,
            model=request.model,
            tenant_id=context.tenant_id,
            gateway_key_id=context.gateway_key_id,
            request_id=request.request_id,
            response_id=record.response_id,
        )

    async def _record_replay(
        self, request: KemoRequest, context: RequestContext, record: ExecutionRecord
    ) -> None:
        if self.statistics is not None:
            await self.statistics.record_replay(
                task="llm",
                provider_id=record.provider_id,
                model=request.model,
                gateway_key_id=context.gateway_key_id,
            )

    def _response_from_result(
        self,
        request: KemoRequest,
        record: ExecutionRecord,
        result: ProviderResult,
        context: RequestContext,
    ) -> KemoResponse:
        response = KemoResponse(
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
        invalid_calls = validate_tool_call_output(response.output, request.tools or [])
        if invalid_calls and response.status in {
            "completed",
            "requires_action",
            "incomplete",
        }:
            safe_details = {
                "reason": "invalid_tool_arguments",
                "invalid_tool_calls": invalid_calls,
            }
            response = KemoResponse(
                id=response.id,
                request_id=response.request_id,
                status="incomplete",
                model=response.model,
                output=[
                    item for item in response.output if not isinstance(item, ToolCallItem)
                ],
                usage=response.usage,
                incomplete_details=safe_details,
                provider_response_id=response.provider_response_id,
                metadata=response.metadata,
                extensions=response.extensions,
            )
        self._validate_response_contract(request, response, context)
        return response

    @staticmethod
    def _validate_response_contract(
        request: KemoRequest,
        response: KemoResponse,
        context: RequestContext,
    ) -> None:
        produced_modalities: set[str] = set()
        for item in response.output:
            if not isinstance(item, MessageItem):
                continue
            for block in item.content:
                if isinstance(block, TextContent):
                    produced_modalities.add("text")
                elif isinstance(
                    block, (ImageContent, AudioContent, VideoContent, FileContent)
                ):
                    produced_modalities.add(block.type)
                    GatewayExecutor._validate_output_asset(block, context)

        requested_modalities = set(request.output.modalities)
        unexpected_media = (produced_modalities - {"text"}) - requested_modalities
        if unexpected_media:
            raise ValueError(
                f"Provider 返回了未请求的输出模态: {sorted(unexpected_media)}"
            )
        if response.status == "completed":
            missing = requested_modalities - produced_modalities
            if missing:
                raise ValueError(
                    f"Provider completed 响应缺少请求的输出模态: {sorted(missing)}"
                )

    @staticmethod
    def _validate_output_asset(
        block: ImageContent | AudioContent | VideoContent | FileContent,
        context: RequestContext,
    ) -> None:
        if context.assets is None or block.asset_id is None:
            raise ValueError("Provider 媒体输出必须登记为当前执行上下文的 Asset")
        resolved = context.assets.resolve(block.asset_id)
        descriptor = resolved.descriptor
        if descriptor.purpose != "output":
            raise ValueError("Provider 媒体输出不能引用输入 Asset")
        if descriptor.mime_type != block.mime_type:
            raise ValueError("Provider 媒体输出 MIME 与 Asset 不一致")
        if descriptor.checksum_sha256 != block.checksum_sha256:
            raise ValueError("Provider 媒体输出 SHA-256 与 Asset 不一致")

    @staticmethod
    def _media_item_fingerprints(response: KemoResponse) -> dict[str, str]:
        fingerprints: dict[str, str] = {}
        for item in response.output:
            if not isinstance(item, MessageItem) or not any(
                isinstance(
                    block, (ImageContent, AudioContent, VideoContent, FileContent)
                )
                for block in item.content
            ):
                continue
            fingerprints[item.id] = json.dumps(
                item.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        return fingerprints

    @staticmethod
    def _valid_stream_tool_call(
        request: KemoRequest,
        provider_event: ProviderEvent,
    ) -> bool:
        """Only publish a completed tool event when its call is executable.

        Argument fragments may be streamed before the terminal response, but a
        ``tool_call.completed`` event is an execution boundary for clients.  Do
        not publish malformed or Schema-invalid calls; the terminal response
        conversion will report the safe ``invalid_tool_arguments`` diagnostic.
        """

        if provider_event.kind != ProviderEventKind.TOOL_COMPLETED:
            return True
        if not isinstance(provider_event.item, dict):
            return False
        try:
            item = ToolCallItem.model_validate(provider_event.item)
        except Exception:
            return False
        return not validate_tool_call_output([item], request.tools or [])

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
        lease_owned_by_producer = False
        package_owned_by_producer = False
        package: ProviderPackage | None = None
        try:
            record, created, package = await self.prepare(request, context)
            if not created:
                await self._record_replay(request, context, record)
                if record.response is not None:
                    return record.response
                return await self.store.wait_terminal(record)

            record.status = InternalStatus.RUNNING
            statistics_handle = await self._begin_statistics(request, context, record)
            record.producer_task = asyncio.create_task(
                self._execute_once(
                    request,
                    context,
                    record,
                    package,
                    statistics_handle,
                    lease,
                ),
                name=f"provider-execute:{record.response_id}",
            )
            lease_owned_by_producer = lease is not None
            package_owned_by_producer = True
            await self.store.save(record)
            result = await asyncio.shield(record.producer_task)
            assert isinstance(result, KemoResponse)
            return result
        finally:
            if lease is not None and not lease_owned_by_producer:
                await lease.release()
            if package is not None and not package_owned_by_producer:
                await self.registry.release_registered(package)

    async def _execute_once(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
        package: ProviderPackage,
        statistics_handle: InvocationHandle | None,
        execution_lease: ExecutionLease | None,
    ) -> KemoResponse:
        response: KemoResponse | None = None
        try:
            try:
                async with asyncio.timeout(self.execution_timeout_seconds):
                    result = await package.execute(request, context)
                if statistics_handle is not None:
                    statistics_handle.mark_response()
                response = self._response_from_result(request, record, result, context)
            except TimeoutError:
                result = ProviderResult(
                    status="failed",
                    error=ErrorObject(
                        type="gateway_timeout",
                        code="GATEWAY_TIMEOUT",
                        message="模型执行超过网关允许的最大时间。",
                        retryable=True,
                    ),
                )
                response = self._response_from_result(request, record, result, context)
            except ProviderException as exc:
                result = ProviderResult(status="failed", error=exc.error)
                response = self._response_from_result(request, record, result, context)
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
                response = self._response_from_result(request, record, result, context)
            record.status = InternalStatus(response.status)
            record.response = response
            record.provider_response_id = response.provider_response_id
            await self.store.save(record)
            if self.statistics is not None:
                await self.statistics.finish_invocation(
                    statistics_handle,
                    status=response.status,
                    usage=response.usage,
                    error_code=response.error.code if response.error else None,
                    error_type=response.error.type if response.error else None,
                    error_message=response.error.message if response.error else None,
                    provider_response_id=response.provider_response_id,
                )
            return response
        finally:
            try:
                if execution_lease is not None:
                    await execution_lease.release()
            finally:
                await self.registry.release_registered(package)

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
        prepared: PreparedStream | None = None
        try:
            prepared = await self.prepare_stream(
                request,
                context,
                last_event_id=last_event_id,
                execution_lease=lease,
            )
            async for event in self.iter_prepared_stream(prepared):
                yield event
        finally:
            if prepared is None and lease is not None:
                await lease.release()

    async def prepare_stream(
        self,
        request: KemoRequest,
        context: RequestContext,
        *,
        last_event_id: str | None = None,
        execution_lease: ExecutionLease | None = None,
    ) -> PreparedStream:
        """Prepare replay/idempotency before the HTTP SSE headers are sent."""
        if last_event_id is not None:
            existing = await self.store.get_by_request_id(
                context.tenant_id, request.request_id
            )
            if existing is None:
                raise StreamResumeError("Last-Event-ID 对应的响应不存在或已过期")

        record, created, package = await self.prepare(request, context)
        package_owned_by_producer = False
        try:
            if created:
                record.status = InternalStatus.RUNNING
                created_event = EventAssembler.created(
                    request_id=request.request_id, response_id=record.response_id
                )
                await self.store.append_event(record, created_event)
                statistics_handle = await self._begin_statistics(request, context, record)
                record.producer_task = asyncio.create_task(
                    self._produce_stream(
                        request,
                        context,
                        record,
                        package,
                        execution_lease,
                        statistics_handle,
                    ),
                    name=f"provider-stream:{record.response_id}",
                )
                package_owned_by_producer = True
                await self.store.save(record)
            else:
                await self._record_replay(request, context, record)

            after_sequence = -1
            if last_event_id is not None:
                matches = [
                    event.sequence
                    for event in record.events
                    if event.event_id == last_event_id
                ]
                if not matches:
                    raise StreamResumeError("Last-Event-ID 不属于该响应或已过期")
                after_sequence = matches[0]
            return PreparedStream(
                record=record,
                after_sequence=after_sequence,
                execution_lease=execution_lease,
                lease_owned_by_producer=created and execution_lease is not None,
            )
        finally:
            if package is not None and not package_owned_by_producer:
                await self.registry.release_registered(package)

    async def iter_prepared_stream(
        self, prepared: PreparedStream
    ) -> AsyncIterator[SSEEvent]:
        try:
            async for event in self.store.subscribe(
                prepared.record, prepared.after_sequence
            ):
                yield event
        finally:
            if (
                prepared.execution_lease is not None
                and not prepared.lease_owned_by_producer
            ):
                await prepared.execution_lease.release()

    async def _produce_stream(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
        package: ProviderPackage,
        execution_lease: ExecutionLease | None = None,
        statistics_handle: InvocationHandle | None = None,
    ) -> None:
        try:
            try:
                await asyncio.wait_for(
                    self._produce_stream_inner(
                        request, context, record, package, statistics_handle
                    ),
                    timeout=self.execution_timeout_seconds,
                )
            except TimeoutError:
                if record.response is None:
                    await self._store_stream_failure(
                        request,
                        context,
                        record,
                        ErrorObject(
                            type="gateway_timeout",
                            code="GATEWAY_TIMEOUT",
                            message="模型流式执行超过网关允许的最大时间。",
                            retryable=True,
                        ),
                    )
        finally:
            try:
                if self.statistics is not None:
                    response = record.response
                    await self.statistics.finish_invocation(
                        statistics_handle,
                        status=response.status if response is not None else "incomplete",
                        usage=response.usage if response is not None else None,
                        error_code=(
                            response.error.code
                            if response is not None and response.error is not None
                            else "STREAM_TERMINATED" if response is None else None
                        ),
                        error_type=(
                            response.error.type
                            if response is not None and response.error is not None
                            else "stream_terminated" if response is None else None
                        ),
                        error_message=(
                            response.error.message
                            if response is not None and response.error is not None
                            else "流式响应在终态前终止" if response is None else None
                        ),
                        provider_response_id=record.provider_response_id,
                    )
            except Exception:
                # Statistics are observability only.  Never allow a faulty
                # metrics backend to strand the execution lease or Provider
                # reference after the response has already reached a terminal
                # state.
                logger.warning(
                    "Stream statistics finalization failed for %s",
                    record.response_id,
                )
            finally:
                try:
                    if execution_lease is not None:
                        await execution_lease.release()
                finally:
                    await self.registry.release_registered(package)

    async def _produce_stream_inner(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
        package: ProviderPackage | None = None,
        statistics_handle: InvocationHandle | None = None,
    ) -> None:
        if package is None:
            # Kept for direct internal/test callers.  Production stream
            # producers receive the reference acquired by ``prepare``.
            package = self.registry.resolve_registered(request.model)
        completed_media: dict[str, MessageItem] = {}
        completed_media_fingerprints: dict[str, str] = {}
        pending_tool_events: list[ProviderEvent] = []
        try:
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
                if statistics_handle is not None and provider_event.kind in {
                    ProviderEventKind.ITEM_ADDED,
                    ProviderEventKind.TEXT_DELTA,
                    ProviderEventKind.AUDIO_DELTA,
                    ProviderEventKind.REASONING_SUMMARY_DELTA,
                    ProviderEventKind.REASONING_CONTENT_DELTA,
                    ProviderEventKind.TOOL_ARGUMENTS_DELTA,
                    ProviderEventKind.TOOL_COMPLETED,
                    ProviderEventKind.MEDIA_COMPLETED,
                    ProviderEventKind.COMPLETED,
                    ProviderEventKind.INCOMPLETE,
                    ProviderEventKind.FAILED,
                    ProviderEventKind.CANCELLED,
                }:
                    statistics_handle.mark_response()
                if provider_event.kind == ProviderEventKind.TOOL_COMPLETED:
                    # A tool_call.completed event is an execution boundary.  Hold
                    # the whole batch until the provider terminal response has
                    # been validated so one malformed parallel call cannot leak
                    # earlier valid calls to the client.
                    pending_tool_events.append(provider_event)
                    continue
                terminal_response = None
                terminal_provider_event = provider_event
                if provider_event.kind in {
                    ProviderEventKind.COMPLETED,
                    ProviderEventKind.INCOMPLETE,
                    ProviderEventKind.FAILED,
                    ProviderEventKind.CANCELLED,
                }:
                    if provider_event.result is None:
                        raise RuntimeError("Provider 终态缺少完整 result")
                    terminal_response = self._response_from_result(
                        request, record, provider_event.result, context
                    )
                    terminal_media = self._media_item_fingerprints(terminal_response)
                    if terminal_media != completed_media_fingerprints:
                        raise RuntimeError(
                            "流式媒体完成事件与统一终态中的媒体 Item 不一致"
                        )
                    expected_kind = {
                        "completed": ProviderEventKind.COMPLETED,
                        "requires_action": ProviderEventKind.COMPLETED,
                        "incomplete": ProviderEventKind.INCOMPLETE,
                        "failed": ProviderEventKind.FAILED,
                        "cancelled": ProviderEventKind.CANCELLED,
                    }[terminal_response.status]
                    if expected_kind != provider_event.kind:
                        terminal_provider_event = replace(
                            provider_event,
                            kind=expected_kind,
                        )

                    if terminal_response.status == "requires_action":
                        terminal_calls = [
                            item
                            for item in terminal_response.output
                            if isinstance(item, ToolCallItem)
                        ]
                        pending_by_call_id: dict[str, ProviderEvent] = {}
                        for pending in pending_tool_events:
                            if not self._valid_stream_tool_call(request, pending):
                                continue
                            try:
                                pending_item = ToolCallItem.model_validate(pending.item)
                            except Exception:
                                continue
                            pending_by_call_id.setdefault(
                                pending_item.call_id,
                                pending,
                            )
                        for item in terminal_calls:
                            source = pending_by_call_id.get(item.call_id)
                            source = source or ProviderEvent(
                                kind=ProviderEventKind.TOOL_COMPLETED,
                                item_id=item.id,
                                call_id=item.call_id,
                                name=item.name,
                                item=item.model_dump(mode="python"),
                                provider_response_id=provider_event.provider_response_id,
                            )
                            call_event = EventAssembler.assemble(
                                replace(
                                    source,
                                    kind=ProviderEventKind.TOOL_COMPLETED,
                                    item_id=item.id,
                                    call_id=item.call_id,
                                    name=item.name,
                                    item=item.model_dump(mode="python"),
                                ),
                                request_id=request.request_id,
                                response_id=record.response_id,
                                sequence=record.next_sequence,
                            )
                            await self.store.append_event(record, call_event)
                    pending_tool_events.clear()

                event = EventAssembler.assemble(
                    terminal_provider_event,
                    request_id=request.request_id,
                    response_id=record.response_id,
                    sequence=record.next_sequence,
                    terminal_response=terminal_response,
                )
                if provider_event.kind == ProviderEventKind.MEDIA_COMPLETED:
                    if not isinstance(event.item, MessageItem):
                        raise RuntimeError("媒体完成事件缺少 MessageItem")
                    if event.item.id in completed_media:
                        raise RuntimeError("同一个媒体 Item 重复完成")
                    for block in event.item.content:
                        if isinstance(
                            block,
                            (ImageContent, AudioContent, VideoContent, FileContent),
                        ):
                            self._validate_output_asset(block, context)
                    completed_media[event.item.id] = event.item
                    completed_media_fingerprints[event.item.id] = json.dumps(
                        event.item.model_dump(mode="json", exclude_none=True),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                await self.store.append_event(record, event)
                if terminal_response is not None:
                    return
        except asyncio.CancelledError:
            raise
        except ProviderException as exc:
            result = ProviderResult(
                status="failed",
                output=[
                    item.model_dump(mode="python") for item in completed_media.values()
                ],
                error=exc.error,
            )
            response = self._response_from_result(request, record, result, context)
            failed = EventAssembler.assemble(
                ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=exc.error),
                request_id=request.request_id,
                response_id=record.response_id,
                sequence=record.next_sequence,
                terminal_response=response,
            )
            await self.store.append_event(record, failed)
            return
        except Exception as exc:
            error = ErrorObject(
                type="adapter_contract_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider stream violated the adapter contract.",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            )
            result = ProviderResult(
                status="failed",
                output=[
                    item.model_dump(mode="python") for item in completed_media.values()
                ],
                error=error,
            )
            response = self._response_from_result(request, record, result, context)
            failed = EventAssembler.assemble(
                ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=error),
                request_id=request.request_id,
                response_id=record.response_id,
                sequence=record.next_sequence,
                terminal_response=response,
            )
            await self.store.append_event(record, failed)
            return

        if record.response is None:
            error = ErrorObject(
                type="gateway_protocol_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider 流在统一终态之前结束。",
                retryable=True,
            )
            result = ProviderResult(
                status="failed",
                output=[
                    item.model_dump(mode="python") for item in completed_media.values()
                ],
                usage=Usage(),
                error=error,
            )
            response = self._response_from_result(request, record, result, context)
            failed = EventAssembler.assemble(
                ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=error),
                request_id=request.request_id,
                response_id=record.response_id,
                sequence=record.next_sequence,
                terminal_response=response,
            )
            await self.store.append_event(record, failed)

    async def _store_stream_failure(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
        error: ErrorObject,
    ) -> None:
        if record.response is not None:
            return
        result = ProviderResult(status="failed", error=error)
        response = self._response_from_result(request, record, result, context)
        failed = EventAssembler.assemble(
            ProviderEvent(kind=ProviderEventKind.FAILED, result=result, error=error),
            request_id=request.request_id,
            response_id=record.response_id,
            sequence=record.next_sequence,
            terminal_response=response,
        )
        await self.store.append_event(record, failed)

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

        package = self.registry.acquire_registered(record.model)
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
            gateway_key_id=None,
            assets=(
                self.assets.bind(tenant_id, subject_id)
                if self.assets is not None
                else None
            ),
        )
        try:
            await package.cancel(record.provider_response_id, context)
        finally:
            await self.registry.release_registered(package)
        partial_output: list[MessageItem] = []
        seen_items: set[str] = set()
        for event in (*record.events, *record.pending_events):
            if (
                event.type == "output_media.completed"
                and isinstance(event.item, MessageItem)
                and event.item.id not in seen_items
            ):
                seen_items.add(event.item.id)
                partial_output.append(event.item)
        response = KemoResponse(
            id=record.response_id,
            request_id=record.request_id,
            status="cancelled",
            model=record.model,
            output=partial_output,
        )
        cancelled = EventAssembler.assemble(
            ProviderEvent(
                kind=ProviderEventKind.CANCELLED,
                result=ProviderResult(
                    status="cancelled",
                    output=[
                        item.model_dump(mode="python") for item in partial_output
                    ],
                ),
            ),
            request_id=record.request_id,
            response_id=record.response_id,
            sequence=record.next_sequence,
            terminal_response=response,
        )
        await self.store.append_event(record, cancelled)
        if record.producer_task is not None and not record.producer_task.done():
            record.producer_task.cancel()
        return response
