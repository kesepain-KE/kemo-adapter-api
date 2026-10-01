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
from core.event_assembler import EventAssembler, EventTooLargeError
from core.live_config import LiveConfigManager
from core.models import (
    AudioContent,
    ErrorObject,
    FileContent,
    ImageContent,
    JsonContent,
    KemoRequest,
    KemoResponse,
    KemoResponseBatch,
    MessageItem,
    SSEEvent,
    TextContent,
    RefusalContent,
    ToolCallItem,
    Usage,
    VideoContent,
)
from core.provider_contract import (
    ProviderEvent,
    ProviderBatchResult,
    ProviderChoiceResult,
    ProviderEventKind,
    ProviderException,
    ProviderPackage,
    ProviderResult,
    RequestContext,
)
from provider.protocol.streaming import (
    MessageItemStart,
    ReasoningItemStart,
    StreamSequenceGuard,
    ToolCallItemStart,
)
from provider.protocol.serialization import request_fingerprint
from core.registry import ProviderRegistry
from core.runtime_state import ExecutionLease, GatewayRuntimeState
from core.stores import ExecutionRecord, ExecutionStore, InternalStatus
from core.tool_arguments import validate_tool_call_output
from storage.statistics import InvocationHandle, StatisticsStore


logger = logging.getLogger(__name__)


def canonical_request_hash(request: KemoRequest) -> str:
    """Use the exact Agent-owned Kemo 2.0 idempotency fingerprint."""

    return request_fingerprint(request)


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

    async def _append_stream_event(
        self, record: ExecutionRecord, event: SSEEvent
    ) -> None:
        """Validate the complete gateway stream before durable publication."""

        guard = record.stream_guard
        if guard is None:
            guard = StreamSequenceGuard()
            for previous in [*record.events, *record.pending_events]:
                guard.accept(previous)
            record.stream_guard = guard
        guard.accept(event)
        await self.store.append_event(record, event)

    @staticmethod
    def _item_start_for(provider_event: ProviderEvent):
        """Build the required identity-only ``output_item.added`` snapshot."""

        item_id = provider_event.item_id
        raw_item = provider_event.item or {}
        if not item_id and isinstance(raw_item, dict):
            item_id = raw_item.get("id")
        if not isinstance(item_id, str):
            return None
        item_type = raw_item.get("type") if isinstance(raw_item, dict) else None
        if item_type == "message" or item_id.startswith("msg_"):
            return MessageItemStart(
                id=item_id,
                phase=(raw_item.get("phase") if isinstance(raw_item, dict) else None),
            )
        if item_type == "reasoning" or item_id.startswith("rs_"):
            return ReasoningItemStart(id=item_id)
        if item_type == "tool_call" or item_id.startswith("call_"):
            call_id = provider_event.call_id or (
                raw_item.get("call_id") if isinstance(raw_item, dict) else None
            )
            name = provider_event.name or (
                raw_item.get("name") if isinstance(raw_item, dict) else None
            )
            if isinstance(call_id, str) and isinstance(name, str) and call_id and name:
                return ToolCallItemStart(id=item_id, call_id=call_id, name=name)
        return None

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
                candidate_response_ids=[
                    context.response_id,
                    *[
                        f"resp_{uuid4().hex}"
                        for _ in range(request.generation.n - 1)
                    ],
                ],
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
                self.registry.bind_execution(resolved.response_id, package)
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
        *,
        response_id: str | None = None,
        choice_index: int = 0,
        choice_count: int = 1,
    ) -> KemoResponse:
        response = KemoResponse(
            protocol_version=request.protocol_version,
            id=response_id or record.response_id,
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
            choice_index=choice_index,
            choice_count=choice_count,
        )
        # Tool choice is an execution contract, not merely a request hint.
        # Validate the provider's complete output before any tool can be
        # exposed to the caller or executed by a continuation.
        calls = [item for item in response.output if isinstance(item, ToolCallItem)]
        choice_mode = getattr(request.tool_choice.mode, "value", request.tool_choice.mode)
        if choice_mode == "none" and calls:
            response = self._tool_contract_incomplete(
                response, request, reason="tool_call_forbidden"
            )
            calls = []
        allowed_names = set(request.tool_choice.allowed_tools or [])
        if choice_mode == "named" and calls:
            allowed_names = {request.tool_choice.name}
        if choice_mode == "allowed" and calls:
            invalid_choice = [item.name for item in calls if item.name not in allowed_names]
            if invalid_choice:
                response = self._tool_contract_incomplete(
                    response, request, reason="tool_choice_violation",
                    details={"invalid_tools": sorted(set(invalid_choice))},
                )
                calls = []
        if choice_mode in {"required", "named"} or (
            choice_mode == "allowed"
            and request.tool_choice.allowed_mode == "required"
        ):
            if not calls and response.status in {"completed", "requires_action"}:
                response = self._tool_contract_incomplete(
                    response, request, reason="missing_tool_call"
                )
        if (
            calls
            and not request.parallel_tool_calls
            and len(calls) > 1
        ):
            response = self._tool_contract_incomplete(
                response, request, reason="parallel_tool_calls_forbidden"
            )
        if calls and request.parallel_tool_calls:
            limit = getattr(request, "extensions", {}).get("max_parallel_tools")
            if isinstance(limit, int) and len(calls) > limit:
                response = self._tool_contract_incomplete(
                    response, request, reason="parallel_tool_limit",
                    details={"count": len(calls), "limit": limit},
                )
        invalid_calls = validate_tool_call_output(response.output, request.tools or [])
        if invalid_calls and response.status in {
            "completed",
            "requires_action",
            "incomplete",
        }:
            safe_details = {
                "reason": "invalid_tool_arguments",
                "details": {"invalid_tool_calls": invalid_calls},
            }
            response = KemoResponse(
                protocol_version=request.protocol_version,
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
                choice_index=choice_index,
                choice_count=choice_count,
            )
        self._validate_response_contract(request, response, context)
        return response

    @staticmethod
    def _tool_contract_incomplete(
        response: KemoResponse,
        request: KemoRequest,
        *,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> KemoResponse:
        known_reasons = {
            "invalid_tool_arguments", "missing_tool_call", "cancelled",
            "output_truncated", "content_filtered", "empty_output", "upstream_stopped",
        }
        wire_reason = reason if reason in known_reasons else "other"
        diagnostic = {"kind": reason}
        if details:
            diagnostic.update(details)
        return KemoResponse(
            protocol_version=request.protocol_version,
            id=response.id,
            request_id=response.request_id,
            status="incomplete",
            model=response.model,
            output=[item for item in response.output if not isinstance(item, ToolCallItem)],
            usage=response.usage,
            incomplete_details={"reason": wire_reason, "details": diagnostic},
            provider_response_id=response.provider_response_id,
            metadata=response.metadata,
            extensions=response.extensions,
            choice_index=response.choice_index,
            choice_count=response.choice_count,
        )

    def _batch_from_result(
        self,
        request: KemoRequest,
        record: ExecutionRecord,
        result: ProviderBatchResult,
        context: RequestContext,
    ) -> KemoResponseBatch:
        expected = request.generation.n
        if expected < 2:
            raise ValueError("Provider 返回 batch，但请求 n=1")
        if len(result.responses) != expected:
            raise ValueError("Provider batch 候选数量与 request.generation.n 不一致")
        by_index: dict[int, ProviderResult] = {}
        for choice in result.responses:
            if not isinstance(choice, ProviderChoiceResult):
                raise ValueError("Provider batch 候选必须是 ProviderChoiceResult")
            if choice.choice_index in by_index:
                raise ValueError("Provider batch choice_index 重复")
            if not 0 <= choice.choice_index < expected:
                raise ValueError("Provider batch choice_index 越界")
            by_index[choice.choice_index] = choice.result
        if set(by_index) != set(range(expected)):
            raise ValueError("Provider batch choice_index 不连续")
        responses = [
            self._response_from_result(
                request,
                record,
                by_index[index],
                context,
                response_id=record.candidate_response_ids[index],
                choice_index=index,
                choice_count=expected,
            )
            for index in range(expected)
        ]
        return KemoResponseBatch(
            protocol_version=request.protocol_version,
            request_id=request.request_id,
            model=request.model,
            responses=responses,
            usage=result.usage,
        )

    def _failed_batch(
        self,
        request: KemoRequest,
        record: ExecutionRecord,
        failure: ProviderResult,
        context: RequestContext,
    ) -> KemoResponseBatch:
        """Preserve preallocated candidate IDs when the shared call fails."""

        count = request.generation.n
        responses = [
            self._response_from_result(
                request,
                record,
                ProviderResult(
                    status="failed",
                    error=failure.error,
                    provider_response_id=failure.provider_response_id,
                ),
                context,
                response_id=record.candidate_response_ids[index],
                choice_index=index,
                choice_count=count,
            )
            for index in range(count)
        ]
        return KemoResponseBatch(
            protocol_version=request.protocol_version,
            request_id=request.request_id,
            model=request.model,
            responses=responses,
            usage=failure.usage,
            extensions={"kemo.shared_failure": True},
        )

    @staticmethod
    def _raise_shared_batch_failure(batch: KemoResponseBatch) -> None:
        if batch.extensions.get("kemo.shared_failure") is not True:
            return
        error = batch.responses[0].error if batch.responses else None
        if error is None:
            error = ErrorObject(
                type="adapter_contract_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider batch execution failed.",
                retryable=False,
            )
        raise ProviderException(error)

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
            if item.refusal or any(isinstance(block, (TextContent, JsonContent, RefusalContent)) for block in item.content):
                produced_modalities.add("text")
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
            if not produced_modalities:
                raise ValueError("Provider completed 响应没有可显示输出")

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
    ) -> KemoResponse | KemoResponseBatch:
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
                if record.batch is not None:
                    self._raise_shared_batch_failure(record.batch)
                    return record.batch
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
            assert isinstance(result, (KemoResponse, KemoResponseBatch))
            if isinstance(result, KemoResponseBatch):
                self._raise_shared_batch_failure(result)
            return result
        finally:
            if lease is not None and not lease_owned_by_producer:
                await lease.release()
            if package is not None and not package_owned_by_producer:
                self.registry.unbind_execution(record.response_id, package)
                await self.registry.release_registered(package)

    async def _execute_once(
        self,
        request: KemoRequest,
        context: RequestContext,
        record: ExecutionRecord,
        package: ProviderPackage,
        statistics_handle: InvocationHandle | None,
        execution_lease: ExecutionLease | None,
    ) -> KemoResponse | KemoResponseBatch:
        response: KemoResponse | None = None
        batch: KemoResponseBatch | None = None
        try:
            try:
                async with asyncio.timeout(self.execution_timeout_seconds):
                    result = await package.execute(request, context)
                if statistics_handle is not None:
                    statistics_handle.mark_response()
                if request.generation.n > 1:
                    if not isinstance(result, ProviderBatchResult):
                        raise ValueError("n>1 Provider 必须返回 ProviderBatchResult")
                    batch = self._batch_from_result(request, record, result, context)
                else:
                    if not isinstance(result, ProviderResult):
                        raise ValueError("n=1 Provider 必须返回 ProviderResult")
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
                if request.generation.n > 1:
                    batch = self._failed_batch(request, record, result, context)
                else:
                    response = self._response_from_result(request, record, result, context)
            except ProviderException as exc:
                result = ProviderResult(status="failed", error=exc.error)
                if request.generation.n > 1:
                    batch = self._failed_batch(request, record, result, context)
                else:
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
                if request.generation.n > 1:
                    batch = self._failed_batch(request, record, result, context)
                else:
                    response = self._response_from_result(request, record, result, context)
            if record.status == InternalStatus.CANCELLED and record.batch is not None:
                return record.batch
            if batch is not None:
                record.status = InternalStatus.COMPLETED
                record.batch = batch
                record.response = batch.responses[0]
                record.provider_response_id = None
            else:
                assert response is not None
                record.status = InternalStatus(response.status)
                record.response = response
                record.provider_response_id = response.provider_response_id
            await self.store.save(record)
            if self.statistics is not None:
                statistics_usage = batch.usage if batch is not None else response.usage
                statistics_status = "completed" if batch is not None else response.status
                statistics_error = None if batch is not None else response.error
                await self.statistics.finish_invocation(
                    statistics_handle,
                    status=statistics_status,
                    usage=statistics_usage,
                    error_code=statistics_error.code if statistics_error else None,
                    error_type=statistics_error.type if statistics_error else None,
                    error_message=statistics_error.message if statistics_error else None,
                    provider_response_id=(response.provider_response_id if batch is None else None),
                )
            return batch or response
        finally:
            try:
                if execution_lease is not None:
                    await execution_lease.release()
            finally:
                self.registry.unbind_execution(record.response_id, package)
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
                await self._append_stream_event(record, created_event)
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
                self.registry.unbind_execution(record.response_id, package)
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
                    self.registry.unbind_execution(record.response_id, package)
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
        started_item_ids: set[str] = set()
        text_buffers: dict[tuple[str, int], str] = {}
        closed_text_blocks: set[tuple[str, int]] = set()
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
                    ProviderEventKind.TEXT_DONE,
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
                    ProviderEventKind.ERROR,
                }:
                    statistics_handle.mark_response()

                # Provider packages commonly expose only text deltas.  Kemo
                # 2.0 nevertheless requires an explicit output_text.done
                # before usage and terminal frames.  Materialize that closure
                # in the gateway using the exact aggregate seen on the wire.
                if provider_event.kind == ProviderEventKind.TEXT_DELTA:
                    if provider_event.item_id is not None and provider_event.content_index is not None:
                        key = (provider_event.item_id, provider_event.content_index)
                        text_buffers[key] = text_buffers.get(key, "") + (provider_event.delta or "")
                if provider_event.kind == ProviderEventKind.TEXT_DONE:
                    if provider_event.item_id is not None and provider_event.content_index is not None:
                        closed_text_blocks.add(
                            (provider_event.item_id, provider_event.content_index)
                        )

                if provider_event.kind in {
                    ProviderEventKind.USAGE,
                    ProviderEventKind.COMPLETED,
                    ProviderEventKind.INCOMPLETE,
                    ProviderEventKind.FAILED,
                    ProviderEventKind.CANCELLED,
                    ProviderEventKind.ERROR,
                }:
                    for (item_id, content_index), text in list(text_buffers.items()):
                        key = (item_id, content_index)
                        if key in closed_text_blocks:
                            continue
                        done_event = EventAssembler.assemble(
                            ProviderEvent(
                                kind=ProviderEventKind.TEXT_DONE,
                                item_id=item_id,
                                content_index=content_index,
                                text=text,
                                provider_response_id=provider_event.provider_response_id,
                            ),
                            request_id=request.request_id,
                            response_id=record.response_id,
                            sequence=record.next_sequence,
                        )
                        await self._append_stream_event(record, done_event)
                        closed_text_blocks.add(key)

                # Provider packages deliberately emit an unwrapped event stream.
                # Materialize the protocol 2.0 identity snapshot before any
                # delta/done/completed payload, including providers that do not
                # have a native "item added" event.
                if provider_event.kind != ProviderEventKind.USAGE:
                    item_start = self._item_start_for(provider_event)
                    # Do not publish an identity frame for a delta that is
                    # already over the complete-event budget: protocol 2.0
                    # requires a compact failure rather than a half-response.
                    if provider_event.delta is not None and len(
                        provider_event.delta.encode("utf-8")
                    ) > 1024 * 1024 - 4096:
                        item_start = None
                    if item_start is not None and item_start.id not in started_item_ids:
                        start_event = EventAssembler.assemble(
                            ProviderEvent(
                                kind=ProviderEventKind.ITEM_ADDED,
                                item_id=item_start.id,
                                item=item_start.model_dump(mode="python"),
                                provider_response_id=provider_event.provider_response_id,
                            ),
                            request_id=request.request_id,
                            response_id=record.response_id,
                            sequence=record.next_sequence,
                        )
                        await self._append_stream_event(record, start_event)
                        started_item_ids.add(item_start.id)
                if provider_event.kind == ProviderEventKind.ITEM_ADDED:
                    # The gateway has emitted the normalized identity-only
                    # snapshot above; never forward a provider's full item as
                    # an ``output_item.added`` payload.
                    continue
                if provider_event.kind == ProviderEventKind.TOOL_COMPLETED:
                    # A tool_call.completed event is an execution boundary.  Hold
                    # the whole batch until the provider terminal response has
                    # been validated so one malformed parallel call cannot leak
                    # earlier valid calls to the client.
                    pending_tool_events.append(provider_event)
                    continue
                terminal_response = None
                terminal_provider_event = provider_event
                if provider_event.kind == ProviderEventKind.ERROR:
                    if provider_event.error is None:
                        raise RuntimeError("standalone error 事件缺少 error")
                    standalone = EventAssembler.assemble(
                        provider_event,
                        request_id=request.request_id,
                        response_id=record.response_id,
                        sequence=record.next_sequence,
                    )
                    await self._append_stream_event(record, standalone)
                    # Keep a durable failed snapshot for GET/idempotency while
                    # preserving the wire rule that this stream terminates on
                    # the standalone error event (without response payload).
                    result = ProviderResult(status="failed", error=provider_event.error)
                    record.response = self._response_from_result(
                        request, record, result, context
                    )
                    record.status = InternalStatus.FAILED
                    await self.store.save(record)
                    return
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
                            await self._append_stream_event(record, call_event)
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
                await self._append_stream_event(record, event)
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
            await self._append_stream_event(record, failed)
            return
        except Exception as exc:
            event_too_large = isinstance(exc, EventTooLargeError)
            error = ErrorObject(
                type="adapter_contract_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider stream violated the adapter contract.",
                retryable=True,
                details=(
                    {"kind": "event_too_large"}
                    if event_too_large
                    else {"exception_type": type(exc).__name__}
                ),
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
            await self._append_stream_event(record, failed)
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
            await self._append_stream_event(record, failed)

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
        await self._append_stream_event(record, failed)

    async def get(self, tenant_id: str, response_id: str) -> KemoResponse | None:
        record = await self.store.get_by_response_id(tenant_id, response_id)
        if record is None:
            return None
        if record.batch is not None:
            return next(
                (item for item in record.batch.responses if item.id == response_id),
                None,
            )
        if record.response is not None:
            return record.response
        return KemoResponse(
            protocol_version="2.0",
            id=response_id,
            request_id=record.request_id,
            status="incomplete",
            model=record.model,
            incomplete_details={"reason": "running"},
            completed_at=None,
        )

    async def cancel(
        self, *, tenant_id: str, subject_id: str, response_id: str
    ) -> KemoResponse | None:
        record = await self.store.get_by_response_id(tenant_id, response_id)
        if record is None:
            return None
        if record.batch is not None:
            return next(
                (item for item in record.batch.responses if item.id == response_id),
                None,
            )
        if record.response is not None:
            return record.response

        if len(record.candidate_response_ids) > 1:
            package = self.registry.acquire_registered(
                record.model, response_id=record.response_id
            )
            context = RequestContext(
                tenant_id=tenant_id,
                subject_id=subject_id,
                request_id=record.request_id,
                response_id=record.response_id,
                trace_id=f"trace_{uuid4().hex}",
                gateway_system_prompt=(
                    self.live_config.current.gateway_system_prompt
                    if self.live_config
                    else ""
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
            count = len(record.candidate_response_ids)
            responses = [
                KemoResponse(
                    protocol_version="2.0",
                    id=candidate_id,
                    request_id=record.request_id,
                    status="cancelled",
                    model=record.model,
                    choice_index=index,
                    choice_count=count,
                )
                for index, candidate_id in enumerate(record.candidate_response_ids)
            ]
            record.batch = KemoResponseBatch(
                protocol_version="2.0",
                request_id=record.request_id,
                model=record.model,
                responses=responses,
            )
            record.response = responses[0]
            record.status = InternalStatus.CANCELLED
            await self.store.save(record)
            if record.producer_task is not None and not record.producer_task.done():
                record.producer_task.cancel()
            return next(item for item in responses if item.id == response_id)

        package = self.registry.acquire_registered(
            record.model, response_id=record.response_id
        )
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
            protocol_version="2.0",
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
        await self._append_stream_event(record, cancelled)
        if record.producer_task is not None and not record.producer_task.done():
            record.producer_task.cancel()
        return response
