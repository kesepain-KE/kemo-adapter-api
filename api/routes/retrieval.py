"""面向 kemo-graph 的向量化与重排序公开接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from api.dependencies import get_retrieval_executor
from api.middleware import Principal, authenticated_principal
from core.models import (
    EmbeddingRequest,
    EmbeddingResponse,
    ErrorObject,
    RerankRequest,
    RerankResponse,
)
from core.retrieval_executor import ModelOperationFailure, RetrievalExecutor
from core.runtime_state import GatewayDrainingError
from core.stores import IdempotencyConflict


router = APIRouter(prefix="/model", tags=["retrieval"])


def require_embedding_scope(
    principal: Principal = Depends(authenticated_principal),
) -> Principal:
    if not principal.scopes.intersection({"model:invoke", "embedding:invoke", "owner"}):
        raise HTTPException(status_code=403, detail="需要 embedding:invoke 权限")
    return principal


def require_rerank_scope(
    principal: Principal = Depends(authenticated_principal),
) -> Principal:
    if not principal.scopes.intersection({"model:invoke", "rerank:invoke", "owner"}):
        raise HTTPException(status_code=403, detail="需要 rerank:invoke 权限")
    return principal


def validate_headers(
    request_id: str,
    protocol_version: str,
    idempotency_key: str | None,
) -> None:
    if idempotency_key != request_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key 必须等于 request_id")
    if protocol_version != "1.0":
        raise HTTPException(status_code=400, detail="协议版本不兼容")


def unknown_model(request_id: str, model: str) -> ModelOperationFailure:
    return ModelOperationFailure(
        request_id,
        ErrorObject(
            type="model_not_found",
            code="MODEL_NOT_FOUND",
            message=f"未知或不可用模型: {model}",
            retryable=False,
        ),
        404,
    )


@router.post("/embeddings", response_model=EmbeddingResponse)
async def create_embeddings(
    request: EmbeddingRequest,
    principal: Principal = Depends(require_embedding_scope),
    executor: RetrievalExecutor = Depends(get_retrieval_executor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    protocol_version: str | None = Header(default=None, alias="X-Kemo-Protocol-Version"),
) -> EmbeddingResponse:
    validate_headers(request.request_id, protocol_version or "", idempotency_key)
    context = executor.make_context(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        request_id=request.request_id,
    )
    try:
        return await executor.embeddings(request, context)
    except LookupError as exc:
        raise unknown_model(request.request_id, request.model) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="相同 request_id 对应不同请求正文") from exc
    except GatewayDrainingError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc


@router.post("/rerank", response_model=RerankResponse)
async def create_rerank(
    request: RerankRequest,
    principal: Principal = Depends(require_rerank_scope),
    executor: RetrievalExecutor = Depends(get_retrieval_executor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    protocol_version: str | None = Header(default=None, alias="X-Kemo-Protocol-Version"),
) -> RerankResponse:
    validate_headers(request.request_id, protocol_version or "", idempotency_key)
    context = executor.make_context(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        request_id=request.request_id,
    )
    try:
        return await executor.rerank(request, context)
    except LookupError as exc:
        raise unknown_model(request.request_id, request.model) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="相同 request_id 对应不同请求正文") from exc
    except GatewayDrainingError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
