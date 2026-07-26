"""把无信封 ProviderEvent 统一组装成 Kemo SSEEvent。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from core.models import KemoResponse, SSEEvent
from core.provider_contract import ProviderEvent, ProviderEventKind


_TERMINAL_EVENT_TYPES = {
    ProviderEventKind.COMPLETED: "response.completed",
    ProviderEventKind.INCOMPLETE: "response.incomplete",
    ProviderEventKind.FAILED: "response.failed",
    ProviderEventKind.CANCELLED: "response.cancelled",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventAssembler:
    """Provider 包无权设置 sequence、event_id 或公共终态。"""

    @staticmethod
    def created(*, request_id: str, response_id: str, sequence: int = 0) -> SSEEvent:
        return SSEEvent(
            type="response.created",
            event_id=f"evt_{uuid4().hex}",
            sequence=sequence,
            request_id=request_id,
            response_id=response_id,
            timestamp=_now(),
        )

    @staticmethod
    def assemble(
        provider_event: ProviderEvent,
        *,
        request_id: str,
        response_id: str,
        sequence: int,
        terminal_response: KemoResponse | None = None,
    ) -> SSEEvent:
        event_type = _TERMINAL_EVENT_TYPES.get(provider_event.kind, provider_event.kind.value)
        if provider_event.kind in _TERMINAL_EVENT_TYPES and terminal_response is None:
            raise ValueError("Provider 终态事件必须先由核心组装完整 KemoResponse")
        return SSEEvent(
            type=event_type,
            event_id=f"evt_{uuid4().hex}",
            sequence=sequence,
            request_id=request_id,
            response_id=response_id,
            timestamp=_now(),
            item_id=provider_event.item_id,
            content_index=provider_event.content_index,
            call_id=provider_event.call_id,
            name=provider_event.name,
            delta=provider_event.delta,
            item=provider_event.item,
            usage=provider_event.usage,
            response=terminal_response,
            error=provider_event.error,
            data=provider_event.data,
        )
