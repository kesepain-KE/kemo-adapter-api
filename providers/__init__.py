"""厂商包命名空间与公共错误信息清理工具。"""

from __future__ import annotations

import re


_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_API_KEY_RE = re.compile(
    r"(?i)(\bapi[_ -]?key(?:\s+(?:provided|is|was|:))?\s*[:=]?\s*)[^\s,;]+"
)
_TOKEN_QUERY_RE = re.compile(r"(?i)([?&](?:api[_-]?key|token)=)[^&\s]+")
_HTML_MARKER_RE = re.compile(r"(?is)<(?:!doctype\s+html|html\b)")


def sanitize_provider_message(message: object, *, default: str) -> str:
    """Bound and redact Provider error text before it crosses the gateway."""

    text = str(message or "").strip() or default
    html_marker = _HTML_MARKER_RE.search(text)
    if html_marker is not None:
        prefix = text[: html_marker.start()].strip()
        html_notice = "上游返回 HTML 错误页（非 JSON 响应），请检查上游服务与认证状态"
        text = f"{prefix} {html_notice}" if prefix else html_notice
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _API_KEY_RE.sub(r"\1[REDACTED]", text)
    text = _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", text)
    return text[:200]
