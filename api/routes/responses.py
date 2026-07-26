from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from api.dependencies import get_executor
from api.middleware import Principal, authenticated_principal, control_plane_principal
from api.sse import encode_sse
from core.executor import GatewayExecutor
from core.models import KemoRequest, KemoResponse
from core.stores import IdempotencyConflict
from core.runtime_state import GatewayDrainingError


router = APIRouter(tags=["responses"])


@router.post("/model/responses", response_model=KemoResponse)
async def create_response(
    request: KemoRequest,
    http_request: Request,
    principal: Principal = Depends(authenticated_principal),
    executor: GatewayExecutor = Depends(get_executor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    protocol_version: str | None = Header(default=None, alias="X-Kemo-Protocol-Version"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> KemoResponse | StreamingResponse:
    if idempotency_key != request.request_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key 必须等于 request_id")
    if protocol_version != request.protocol_version or request.protocol_version != "1.0":
        raise HTTPException(status_code=400, detail="协议版本不兼容")
    try:
        execution_lease = await http_request.app.state.runtime_state.admit_execution()
    except GatewayDrainingError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    context = executor.make_context(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        request_id=request.request_id,
    )
    try:
        if request.stream:
            async def body():
                async for event in executor.stream(
                    request,
                    context,
                    last_event_id=last_event_id,
                    execution_lease=execution_lease,
                ):
                    yield encode_sse(event)

            return StreamingResponse(
                body(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return await executor.execute(request, context, execution_lease=execution_lease)
    except LookupError as exc:
        await execution_lease.release()
        raise HTTPException(status_code=404, detail=f"未知模型: {request.model}") from exc
    except IdempotencyConflict as exc:
        await execution_lease.release()
        raise HTTPException(status_code=409, detail="相同 request_id 对应不同请求正文") from exc
    except Exception:
        await execution_lease.release()
        raise


@router.get("/model/responses/{response_id}", response_model=KemoResponse)
async def get_response(
    response_id: str,
    response: Response,
    principal: Principal = Depends(control_plane_principal),
    executor: GatewayExecutor = Depends(get_executor),
) -> KemoResponse:
    result = await executor.get(principal.tenant_id, response_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Response 不存在")
    if result.status == "incomplete" and result.incomplete_details == {"reason": "running"}:
        response.status_code = status.HTTP_202_ACCEPTED
    return result


@router.post("/model/responses/{response_id}/cancel", response_model=KemoResponse)
async def cancel_response(
    response_id: str,
    principal: Principal = Depends(control_plane_principal),
    executor: GatewayExecutor = Depends(get_executor),
) -> KemoResponse:
    result = await executor.cancel(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        response_id=response_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Response 不存在")
    return result
