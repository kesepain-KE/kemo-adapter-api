"""厂商异常、HTTP 状态和错误体的统一映射与脱敏。"""

from __future__ import annotations

from typing import Any

from core.models import ErrorObject


class ExampleErrorMapper:
    def validation_error(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> ErrorObject:
        return ErrorObject(
            type="validation",
            code="VALIDATION_ERROR",
            message=message[:200],
            retryable=False,
            details=details or {},
        )

    def from_exception(self, exc: Exception) -> ErrorObject:
        # TODO: 识别厂商 SDK/HTTP 异常，提取脱敏 request id、retry-after 和 retryable。
        return ErrorObject(
            type="provider_error",
            code="PROVIDER_UNAVAILABLE",
            message="Provider request failed.",
            retryable=True,
            details={"exception_type": type(exc).__name__},
        )
