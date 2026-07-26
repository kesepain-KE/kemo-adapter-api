"""HTTP 异常到统一错误对象的边界转换。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "AUTHENTICATION_ERROR",
            403: "AUTHORIZATION_ERROR",
            404: "RESPONSE_NOT_FOUND",
            409: "IDEMPOTENCY_CONFLICT",
        }.get(exc.status_code, "INTERNAL_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "protocol_version": "1.0",
                "error": {
                    "type": "gateway_error",
                    "code": code,
                    "message": str(exc.detail),
                    "retryable": False,
                    "details": {},
                },
            },
        )
