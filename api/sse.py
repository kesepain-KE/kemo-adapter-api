"""Kemo SSE 的唯一序列化位置。Provider 包不得导入本模块。"""

from __future__ import annotations

import json

from core.models import SSEEvent


def encode_sse(event: SSEEvent) -> bytes:
    data = json.dumps(
        event.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {data}\n\n".encode("utf-8")
