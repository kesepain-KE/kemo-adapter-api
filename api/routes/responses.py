from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from api.dependencies import get_executor
from api.middleware import (
    Principal,
    authenticated_principal,
    control_plane_principal,
    ensure_model_allowed,
    ensure_model_task_allowed,
)
from api.sse import encode_sse
from core.executor import GatewayExecutor, StreamResumeError
from core.models import KemoRequest, KemoResponse, SSEEvent
from core.stores import IdempotencyConflict
from core.runtime_state import GatewayDrainingError, GatewayOverloadedError


router = APIRouter(tags=["responses"])


async def _heartbeat_stream(
    events: AsyncIterator[SSEEvent], heartbeat_seconds: float
) -> AsyncIterator[bytes]:
    """Keep proxy/CDN idle timers alive without inventing protocol events."""
    iterator = events.__aiter__()
    pending: asyncio.Task[SSEEvent] | None = None
    try:
        pending = asyncio.create_task(anext(iterator), name="kemo-sse-next-event")
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield b": kemo-heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield encode_sse(event)
            pending = asyncio.create_task(anext(iterator), name="kemo-sse-next-event")
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


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
    ensure_model_allowed(principal, request.model)
    ensure_model_task_allowed(principal, "llm")
    if idempotency_key != request.request_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key 必须等于 request_id")
    if protocol_version != request.protocol_version or request.protocol_version != "1.0":
        raise HTTPException(status_code=400, detail="协议版本不兼容")
    try:
        execution_lease = await http_request.app.state.runtime_state.admit_execution()
    except (GatewayDrainingError, GatewayOverloadedError) as exc:
        code = (
            "GATEWAY_OVERLOADED"
            if isinstance(exc, GatewayOverloadedError)
            else "GATEWAY_DRAINING"
        )
        raise HTTPException(
            status_code=503,
            detail={"code": code, "message": str(exc), "retryable": True},
            headers={"Retry-After": "5"},
        ) from exc
    context = executor.make_context(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        request_id=request.request_id,
        gateway_key_id=principal.key_id,
    )
    try:
        if request.stream:
            prepared = await executor.prepare_stream(
                request,
                context,
                last_event_id=last_event_id,
                execution_lease=execution_lease,
            )

            async def body():
                async for chunk in _heartbeat_stream(
                    executor.iter_prepared_stream(prepared),
                    http_request.app.state.settings.sse_heartbeat_seconds,
                ):
                    yield chunk

            return StreamingResponse(
                body(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                    "X-Kemo-Heartbeat-Seconds": str(
                        http_request.app.state.settings.sse_heartbeat_seconds
                    ),
                },
            )
        return await executor.execute(request, context, execution_lease=execution_lease)
    except StreamResumeError as exc:
        await execution_lease.release()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STREAM_RESUME_CONFLICT",
                "message": str(exc),
                "retryable": False,
            },
        ) from exc
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
