"""公开 Kemo JSON 请求的流式大小上限。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any


ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """在 JSON 解析前限制请求体，避免先把超大正文读入内存。"""

    def __init__(
        self,
        app: Any,
        *,
        max_bytes: int,
        paths: frozenset[str],
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = paths

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        for key, value in scope.get("headers", []):
            if key.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except (TypeError, ValueError):
                declared = 0
            if declared > self.max_bytes:
                await self._send_rejection(send)
                return

        consumed = 0
        response_started = False

        async def limited_receive() -> ASGIMessage:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: ASGIMessage) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._send_rejection(send)

    async def _send_rejection(self, send: Send) -> None:
        payload = json.dumps(
            {
                "protocol_version": "1.0",
                "error": {
                    "type": "validation",
                    "code": "REQUEST_TOO_LARGE",
                    "message": "Kemo 请求 JSON 超过允许的大小。",
                    "retryable": False,
                    "details": {"limit_bytes": self.max_bytes},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
