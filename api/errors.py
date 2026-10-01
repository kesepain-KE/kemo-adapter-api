"""HTTP 异常到统一错误对象的边界转换。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.retrieval_executor import ModelOperationFailure
from core.assets import AssetStoreFailure
from core.provider_contract import ProviderException
from core.runtime_state import GatewayDrainingError, GatewayOverloadedError


_ERROR_CODES = frozenset({
    "PROTOCOL_ERROR", "UNSUPPORTED_PROTOCOL_VERSION", "VALIDATION_ERROR",
    "TOOL_LINKAGE_ERROR", "STREAM_PROTOCOL_ERROR", "CAPABILITY_ERROR",
    "CAPABILITIES_MODEL_MISMATCH", "MODEL_TASK_MISMATCH",
    "UNSUPPORTED_INPUT_MODALITY", "UNSUPPORTED_OUTPUT_MODALITY",
    "STREAMING_UNSUPPORTED", "TOOLS_UNSUPPORTED", "PARALLEL_TOOLS_UNSUPPORTED",
    "REASONING_UNSUPPORTED", "REASONING_EFFORT_UNSUPPORTED",
    "STRUCTURED_OUTPUT_UNSUPPORTED", "PREDICTION_UNSUPPORTED", "ASSET_ERROR",
    "ASSET_NOT_FOUND", "ASSET_NOT_READY", "ASSET_DELETED", "ASSET_EXPIRED",
    "ASSET_CONTENT_MISSING", "ASSET_DELETE_FAILED", "ASSET_API_UNAVAILABLE",
    "INVALID_MEDIA", "REQUEST_TOO_LARGE", "IDEMPOTENCY_CONFLICT",
    "PROVIDER_UNAVAILABLE", "PROVIDER_BAD_RESPONSE", "PROVIDER_TIMEOUT",
    "GATEWAY_TIMEOUT", "GATEWAY_OVERLOADED", "GATEWAY_DRAINING",
    "STREAM_RESUME_CONFLICT", "CANCELLED", "UNKNOWN_ERROR",
    "AUTHENTICATION_ERROR", "PERMISSION_DENIED", "MODEL_NOT_FOUND",
    "RATE_LIMITED", "QUOTA_EXCEEDED",
})


def _error_envelope(
    error: dict[str, object], *, request_id: str | None = None
) -> dict[str, object]:
    """Build the single Kemo 2.0 HTTP error shape."""

    payload: dict[str, object] = {
        "protocol_version": "2.0",
        "object": "kemo.error",
        "error": error,
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayDrainingError)
    @app.exception_handler(GatewayOverloadedError)
    async def handle_gateway_capacity_error(
        _: Request, exc: GatewayDrainingError | GatewayOverloadedError
    ) -> JSONResponse:
        overloaded = isinstance(exc, GatewayOverloadedError)
        return JSONResponse(
            status_code=503,
            content=_error_envelope({
                    "type": "gateway_capacity",
                    "code": "GATEWAY_OVERLOADED" if overloaded else "GATEWAY_DRAINING",
                    "message": str(exc),
                    "retryable": True,
                    "details": {},
                }),
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(AssetStoreFailure)
    async def handle_asset_store_failure(
        _: Request, exc: AssetStoreFailure
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(exc.error.model_dump(mode="json")),
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
        is_protocol_endpoint = request.url.path.startswith("/model/") or request.url.path.startswith("/assets")
        if not is_protocol_endpoint:
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
            content=_error_envelope({
                    "type": "validation",
                    "code": "VALIDATION_ERROR",
                    "message": "请求不符合 Kemo 2.0 严格协议。",
                    "retryable": False,
                    "details": {"errors": errors},
                }),
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
            content=_error_envelope(error.model_dump(mode="json")),
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
            content=_error_envelope(
                exc.error.model_dump(mode="json"), request_id=exc.request_id
            ),
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
        requested_code = detail_code if isinstance(detail_code, str) else None
        code = requested_code if requested_code in _ERROR_CODES else {
            401: "AUTHENTICATION_ERROR",
            403: "PERMISSION_DENIED",
            404: "PROTOCOL_ERROR",
            409: "IDEMPOTENCY_CONFLICT",
            413: "REQUEST_TOO_LARGE",
            429: "RATE_LIMITED",
            502: "PROVIDER_BAD_RESPONSE",
            503: "PROVIDER_UNAVAILABLE",
            504: "PROVIDER_TIMEOUT",
            499: "CANCELLED",
            500: "UNKNOWN_ERROR",
        }.get(exc.status_code, "INTERNAL_ERROR")
        if code == "INTERNAL_ERROR":
            code = "UNKNOWN_ERROR"
        details = {}
        if requested_code and requested_code not in _ERROR_CODES:
            details["kind"] = requested_code.casefold()
        if isinstance(exc.detail, dict):
            raw_details = exc.detail.get("details")
            if isinstance(raw_details, dict):
                details.update(raw_details)
            if "supported_protocol_versions" in exc.detail:
                details["supported_protocol_versions"] = exc.detail["supported_protocol_versions"]
            if "kind" in exc.detail and isinstance(exc.detail["kind"], str):
                details["kind"] = exc.detail["kind"]
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope({
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
                    "details": details,
                }),
            headers=exc.headers,
        )
