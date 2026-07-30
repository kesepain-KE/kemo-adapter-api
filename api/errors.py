"""HTTP 异常到统一错误对象的边界转换。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.retrieval_executor import ModelOperationFailure
from core.assets import AssetStoreFailure
from core.provider_contract import ProviderException
from core.runtime_state import GatewayDrainingError, GatewayOverloadedError


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayDrainingError)
    @app.exception_handler(GatewayOverloadedError)
    async def handle_gateway_capacity_error(
        _: Request, exc: GatewayDrainingError | GatewayOverloadedError
    ) -> JSONResponse:
        overloaded = isinstance(exc, GatewayOverloadedError)
        return JSONResponse(
            status_code=503,
            content={
                "protocol_version": "1.0",
                "error": {
                    "type": "gateway_capacity",
                    "code": "GATEWAY_OVERLOADED" if overloaded else "GATEWAY_DRAINING",
                    "message": str(exc),
                    "retryable": True,
                    "details": {},
                },
            },
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(AssetStoreFailure)
    async def handle_asset_store_failure(
        _: Request, exc: AssetStoreFailure
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "protocol_version": "1.0",
                "error": exc.error.model_dump(mode="json"),
            },
            headers=(
                {"Retry-After": str(max(1, exc.error.retry_after_ms // 1000))}
                if exc.error.retry_after_ms is not None
                else None
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path != "/model/responses" and not request.url.path.startswith(
            "/assets"
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": [
                        {
                            "location": [str(part) for part in error.get("loc", ())],
                            "message": str(error.get("msg") or "请求字段无效"),
                            "type": str(error.get("type") or "validation_error"),
                        }
                        for error in exc.errors()[:20]
                    ]
                },
            )
        errors = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "message": str(error.get("msg") or "请求字段无效"),
                "type": str(error.get("type") or "validation_error"),
            }
            for error in exc.errors()[:20]
        ]
        return JSONResponse(
            status_code=400,
            content={
                "protocol_version": "1.0",
                "error": {
                    "type": "validation",
                    "code": "VALIDATION_ERROR",
                    "message": "请求不符合 Kemo 1.0 严格协议。",
                    "retryable": False,
                    "details": {"errors": errors},
                },
            },
        )

    @app.exception_handler(ProviderException)
    async def handle_provider_exception(
        _: Request, exc: ProviderException
    ) -> JSONResponse:
        error = exc.error
        status_code = error.provider_status or (
            429
            if error.code == "RATE_LIMITED"
            else 504
            if error.code == "PROVIDER_TIMEOUT"
            else 503
            if error.code in {"PROVIDER_UNAVAILABLE", "ASSET_API_UNAVAILABLE"}
            else 400
            if error.type in {"validation", "capability_validation"}
            or error.code.endswith("_UNSUPPORTED")
            or error.code.startswith("MULTIMODAL_")
            or error.code in {
                "INVALID_MEDIA",
                "REQUEST_TOO_LARGE",
                "UNKNOWN_MULTIMODAL_OPERATION",
            }
            else 502
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "protocol_version": "1.0",
                "error": error.model_dump(mode="json"),
            },
            headers=(
                {"Retry-After": str(max(1, error.retry_after_ms // 1000))}
                if error.retry_after_ms is not None
                else None
            ),
        )

    @app.exception_handler(ModelOperationFailure)
    async def handle_model_operation_failure(
        _: Request, exc: ModelOperationFailure
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "protocol_version": "1.0",
                "request_id": exc.request_id,
                "error": exc.error.model_dump(mode="json"),
            },
            headers=(
                {"Retry-After": str(max(1, exc.error.retry_after_ms // 1000))}
                if exc.error.retry_after_ms is not None
                else None
            ),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        detail_code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        detail_message = exc.detail.get("message") if isinstance(exc.detail, dict) else None
        detail_retryable = (
            exc.detail.get("retryable") if isinstance(exc.detail, dict) else None
        )
        code = detail_code if isinstance(detail_code, str) and detail_code else {
            401: "AUTHENTICATION_ERROR",
            403: "AUTHORIZATION_ERROR",
            404: "RESPONSE_NOT_FOUND",
            409: "IDEMPOTENCY_CONFLICT",
            429: "RATE_LIMITED",
        }.get(exc.status_code, "INTERNAL_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "protocol_version": "1.0",
                "error": {
                    "type": "gateway_error",
                    "code": code,
                    "message": (
                        detail_message
                        if isinstance(detail_message, str) and detail_message
                        else str(exc.detail)
                    ),
                    "retryable": (
                        detail_retryable
                        if isinstance(detail_retryable, bool)
                        else exc.status_code in {408, 425, 429, 502, 503, 504}
                    ),
                    "details": {},
                },
            },
            headers=exc.headers,
        )
