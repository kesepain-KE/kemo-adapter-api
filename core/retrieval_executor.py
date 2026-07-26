"""Embedding 与 Rerank 的统一执行边界；不解释任何厂商私有字段。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from core.live_config import LiveConfigManager
from core.models import (
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    ErrorObject,
    RerankRequest,
    RerankResponse,
    RerankResultItem,
)
from core.provider_contract import (
    ProviderEmbeddingResult,
    ProviderException,
    ProviderRerankResult,
    RequestContext,
)
from core.registry import ProviderRegistry
from core.runtime_state import GatewayRuntimeState
from core.stores import IdempotencyConflict


T = TypeVar("T")


class ModelOperationFailure(Exception):
    """可安全返回调用方的同步模型任务失败。"""

    def __init__(self, request_id: str, error: ErrorObject, status_code: int) -> None:
        super().__init__(error.message)
        self.request_id = request_id
        self.error = error
        self.status_code = status_code


@dataclass(slots=True)
class _OperationRecord:
    request_hash: str
    task: asyncio.Task[Any]


def _request_hash(request: BaseModel) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class RetrievalExecutor:
    """共享路由、Drain、幂等和 Provider 契约校验。"""

    def __init__(
        self,
        registry: ProviderRegistry,
        live_config: LiveConfigManager | None = None,
        runtime_state: GatewayRuntimeState | None = None,
    ) -> None:
        self.registry = registry
        self.live_config = live_config
        self.runtime_state = runtime_state
        self._records: dict[tuple[str, str, str], _OperationRecord] = {}
        self._records_lock = asyncio.Lock()

    def make_context(self, *, tenant_id: str, subject_id: str, request_id: str) -> RequestContext:
        snapshot = self.live_config.current if self.live_config is not None else None
        return RequestContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            request_id=request_id,
            response_id=f"op_{uuid4().hex}",
            trace_id=f"trace_{uuid4().hex}",
            gateway_system_prompt="",
            live_config_revision=snapshot.revision if snapshot else "empty",
        )

    async def _idempotent(
        self,
        operation: str,
        tenant_id: str,
        request_id: str,
        request: BaseModel,
        producer: Callable[[], Awaitable[T]],
    ) -> T:
        key = (operation, tenant_id, request_id)
        digest = _request_hash(request)
        async with self._records_lock:
            record = self._records.get(key)
            if record is not None:
                if record.request_hash != digest:
                    raise IdempotencyConflict("相同 request_id 对应不同请求正文")
                task = record.task
            else:
                task = asyncio.create_task(producer(), name=f"{operation}:{request_id}")
                record = _OperationRecord(request_hash=digest, task=task)
                self._records[key] = record
        try:
            return await asyncio.shield(task)
        except Exception:
            async with self._records_lock:
                if self._records.get(key) is record:
                    self._records.pop(key, None)
            raise

    async def embeddings(
        self, request: EmbeddingRequest, context: RequestContext
    ) -> EmbeddingResponse:
        return await self._idempotent(
            "embedding",
            context.tenant_id,
            request.request_id,
            request,
            lambda: self._embed_once(request, context),
        )

    async def rerank(
        self, request: RerankRequest, context: RequestContext
    ) -> RerankResponse:
        return await self._idempotent(
            "rerank",
            context.tenant_id,
            request.request_id,
            request,
            lambda: self._rerank_once(request, context),
        )

    async def _embed_once(
        self, request: EmbeddingRequest, context: RequestContext
    ) -> EmbeddingResponse:
        package = self.registry.resolve(request.model)
        try:
            capabilities = await package.capabilities(request.model)
        except Exception as exc:
            raise self._provider_contract_failure(request.request_id, exc) from exc
        if capabilities.task != "embedding" or capabilities.embedding is None:
            self._invalid_task(request.request_id, request.model, "embedding")
        embedding_capabilities = capabilities.embedding
        if request.input_type not in embedding_capabilities.input_types:
            self._invalid_request(request.request_id, "模型不支持该 input_type")
        if len(request.inputs) > embedding_capabilities.max_batch_size:
            self._invalid_request(request.request_id, "inputs 超过模型 max_batch_size")
        expected_dimensions = request.dimensions or embedding_capabilities.default_dimensions
        if (
            request.dimensions is not None
            and embedding_capabilities.supported_dimensions
            and request.dimensions not in embedding_capabilities.supported_dimensions
        ):
            self._invalid_request(request.request_id, "模型不支持请求的 dimensions")
        normalization = embedding_capabilities.normalization
        if request.normalize is True and normalization in {"never", "unknown"}:
            self._invalid_request(request.request_id, "模型不能保证归一化向量")
        if request.normalize is False and normalization == "always":
            self._invalid_request(request.request_id, "模型只返回归一化向量")

        result = await self._call_provider_embedding(package, request, context)
        try:
            return self._build_embedding_response(request, result, expected_dimensions)
        except ModelOperationFailure:
            raise
        except Exception as exc:
            raise self._provider_contract_failure(request.request_id, exc) from exc

    async def _rerank_once(
        self, request: RerankRequest, context: RequestContext
    ) -> RerankResponse:
        package = self.registry.resolve(request.model)
        try:
            capabilities = await package.capabilities(request.model)
        except Exception as exc:
            raise self._provider_contract_failure(request.request_id, exc) from exc
        if capabilities.task != "rerank" or capabilities.rerank is None:
            self._invalid_task(request.request_id, request.model, "rerank")
        rerank_capabilities = capabilities.rerank
        if len(request.documents) > rerank_capabilities.max_documents:
            self._invalid_request(request.request_id, "documents 超过模型 max_documents")
        if request.return_documents and not rerank_capabilities.supports_return_documents:
            self._invalid_request(request.request_id, "模型不支持 return_documents")

        result = await self._call_provider_rerank(package, request, context)
        try:
            return self._build_rerank_response(request, result)
        except ModelOperationFailure:
            raise
        except Exception as exc:
            raise self._provider_contract_failure(request.request_id, exc) from exc

    async def _call_provider_embedding(
        self, package: Any, request: EmbeddingRequest, context: RequestContext
    ) -> ProviderEmbeddingResult:
        lease = await self.runtime_state.admit_execution() if self.runtime_state else None
        try:
            try:
                return await package.embed(request, context)
            except ProviderException as exc:
                status_code = 429 if exc.error.provider_status == 429 else 502
                raise ModelOperationFailure(request.request_id, exc.error, status_code) from exc
            except Exception as exc:
                raise self._provider_contract_failure(request.request_id, exc) from exc
        finally:
            if lease is not None:
                await lease.release()

    async def _call_provider_rerank(
        self, package: Any, request: RerankRequest, context: RequestContext
    ) -> ProviderRerankResult:
        lease = await self.runtime_state.admit_execution() if self.runtime_state else None
        try:
            try:
                return await package.rerank(request, context)
            except ProviderException as exc:
                status_code = 429 if exc.error.provider_status == 429 else 502
                raise ModelOperationFailure(request.request_id, exc.error, status_code) from exc
            except Exception as exc:
                raise self._provider_contract_failure(request.request_id, exc) from exc
        finally:
            if lease is not None:
                await lease.release()

    def _build_embedding_response(
        self,
        request: EmbeddingRequest,
        result: ProviderEmbeddingResult,
        expected_dimensions: int,
    ) -> EmbeddingResponse:
        if not isinstance(result, ProviderEmbeddingResult):
            raise self._provider_contract_failure(request.request_id)
        if not result.vector_space_id or not result.vector_space_id.strip():
            raise self._provider_contract_failure(request.request_id)
        by_index = {item.index: item for item in result.embeddings}
        if len(by_index) != len(result.embeddings) or set(by_index) != set(range(len(request.inputs))):
            raise self._provider_contract_failure(request.request_id)

        data: list[EmbeddingData] = []
        for index, source in enumerate(request.inputs):
            raw_vector = by_index[index].vector
            if len(raw_vector) != expected_dimensions:
                raise self._provider_contract_failure(request.request_id)
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in raw_vector
            ):
                raise self._provider_contract_failure(request.request_id)
            data.append(
                EmbeddingData(
                    id=source.id,
                    index=index,
                    vector=[float(value) for value in raw_vector],
                )
            )
        return EmbeddingResponse(
            request_id=request.request_id,
            model=request.model,
            model_version=result.model_version,
            vector_space_id=result.vector_space_id,
            dimensions=expected_dimensions,
            data=data,
            usage=result.usage,
            provider_response_id=result.provider_response_id,
            metadata=result.metadata,
            extensions=result.extensions,
        )

    def _build_rerank_response(
        self, request: RerankRequest, result: ProviderRerankResult
    ) -> RerankResponse:
        if not isinstance(result, ProviderRerankResult) or not result.results:
            raise self._provider_contract_failure(request.request_id)
        seen: set[int] = set()
        validated: list[tuple[int, float]] = []
        for item in result.results:
            if item.index in seen or not 0 <= item.index < len(request.documents):
                raise self._provider_contract_failure(request.request_id)
            if (
                isinstance(item.relevance_score, bool)
                or not isinstance(item.relevance_score, (int, float))
                or not math.isfinite(float(item.relevance_score))
            ):
                raise self._provider_contract_failure(request.request_id)
            seen.add(item.index)
            validated.append((item.index, float(item.relevance_score)))
        top_n = request.top_n or len(request.documents)
        ranked = sorted(validated, key=lambda value: value[1], reverse=True)[:top_n]
        response_items = [
            RerankResultItem(
                rank=rank,
                document_id=request.documents[index].id,
                index=index,
                relevance_score=score,
                document=request.documents[index] if request.return_documents else None,
            )
            for rank, (index, score) in enumerate(ranked, start=1)
        ]
        return RerankResponse(
            request_id=request.request_id,
            model=request.model,
            model_version=result.model_version,
            results=response_items,
            usage=result.usage,
            provider_response_id=result.provider_response_id,
            metadata=result.metadata,
            extensions=result.extensions,
        )

    @staticmethod
    def _invalid_task(request_id: str, model: str, task: str) -> None:
        raise ModelOperationFailure(
            request_id,
            ErrorObject(
                type="invalid_model_task",
                code="MODEL_TASK_MISMATCH",
                message=f"模型 {model} 不是 {task} 模型。",
                retryable=False,
            ),
            400,
        )

    @staticmethod
    def _invalid_request(request_id: str, message: str) -> None:
        raise ModelOperationFailure(
            request_id,
            ErrorObject(
                type="invalid_request",
                code="INVALID_REQUEST",
                message=message,
                retryable=False,
            ),
            400,
        )

    @staticmethod
    def _provider_contract_failure(
        request_id: str, exc: Exception | None = None
    ) -> ModelOperationFailure:
        details = {"exception_type": type(exc).__name__} if exc is not None else {}
        return ModelOperationFailure(
            request_id,
            ErrorObject(
                type="adapter_contract_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider adapter returned an invalid retrieval result.",
                retryable=True,
                details=details,
            ),
            502,
        )
