"""厂商异常、HTTP 状态和错误体的统一映射与脱敏。"""

from __future__ import annotations

from typing import Any

from core.models import ErrorObject
from core.provider_contract import ProviderException


class ExampleErrorMapper:
    """Map vendor failures to a small, safe Kemo error vocabulary.

    Copying this file is not enough to support a vendor.  Replace the status
    and SDK checks with the vendor's documented fields, but keep the rule that
    raw response bodies, headers, URLs, and secrets never enter ``ErrorObject``.
    """

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

    def from_http_status(
        self,
        status: int,
        *,
        retry_after_ms: int | None = None,
        provider_request_id: str | None = None,
        key_failure: bool = False,
    ) -> ErrorObject:
        """Return a conservative error for a vendor HTTP status.

        Provider code should call this after extracting only a documented
        request ID and retry hint.  Do not pass the raw response body or
        headers.  A Provider-specific mapper may refine the code, but it must
        preserve the retry boundary: invalid requests and authentication
        failures are not retryable, while rate limits, timeouts, and 5xx
        failures may be retried by the caller. A 403 is a normal permission
        error unless vendor documentation proves that the credential itself
        is invalid; pass ``key_failure=True`` only in that case so the gateway
        may fail over to another key.
        """

        try:
            normalized = int(status)
        except (TypeError, ValueError):
            normalized = 0

        if normalized == 401:
            code = "AUTHENTICATION_ERROR"
            message = "Provider authentication failed."
            retryable = False
        elif normalized == 403:
            if key_failure:
                code = "AUTHENTICATION_ERROR"
                message = "Provider authentication failed."
            else:
                code = "PERMISSION_DENIED"
                message = "Provider denied this model or operation."
            retryable = False
        elif normalized == 402:
            code = "QUOTA_EXCEEDED"
            message = "Provider quota or balance is unavailable."
            retryable = False
        elif normalized == 408:
            code = "PROVIDER_TIMEOUT"
            message = "Provider request timed out."
            retryable = True
        elif normalized == 429:
            code = "RATE_LIMITED"
            message = "Provider rate limit reached."
            retryable = True
        elif 500 <= normalized <= 599:
            code = "PROVIDER_UNAVAILABLE"
            message = "Provider is temporarily unavailable."
            retryable = True
        elif 400 <= normalized <= 499:
            code = "INVALID_REQUEST"
            message = "Provider rejected the request."
            retryable = False
        else:
            code = "PROVIDER_ERROR"
            message = "Provider request failed."
            retryable = False

        details: dict[str, Any] = {"provider_status": normalized}
        if normalized == 403 and key_failure:
            details["key_failure"] = True
        safe_request_id = str(provider_request_id or "").strip()
        if safe_request_id:
            details["provider_request_id"] = safe_request_id[:128]
        retry_hint = None
        if retry_after_ms is not None:
            try:
                retry_hint = max(0, min(int(retry_after_ms), 86_400_000))
            except (TypeError, ValueError):
                retry_hint = None
        return ErrorObject(
            type="provider_error",
            code=code,
            message=message,
            retryable=retryable,
            retry_after_ms=retry_hint,
            provider_status=normalized or None,
            provider_request_id=safe_request_id[:128] if safe_request_id else None,
            details=details,
        )

    def from_exception(self, exc: Exception) -> ErrorObject:
        """Map an SDK/network exception without echoing its message."""
        if isinstance(exc, ProviderException):
            return exc.error
        if isinstance(exc, (KeyError, TypeError, ValueError)):
            return ErrorObject(
                type="adapter_contract_error",
                code="PROVIDER_BAD_RESPONSE",
                message="Provider returned a response that does not match its adapter contract.",
                retryable=False,
                details={"exception_type": type(exc).__name__},
            )
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return ErrorObject(
                type="provider_error",
                code="PROVIDER_UNAVAILABLE",
                message="Provider request could not be completed.",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            )
        return ErrorObject(
            type="provider_error",
            code="PROVIDER_UNAVAILABLE",
            message="Provider request failed.",
            retryable=False,
            details={"exception_type": type(exc).__name__},
        )
