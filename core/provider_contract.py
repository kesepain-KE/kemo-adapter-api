"""协议适配器与 Kemo 执行器共享的规范化结果、事件和请求上下文。

关键边界：Adapter 输出标准化事件；SSE 字节与 Kemo 事件信封由执行器组装。
sequence、event_id、重放和唯一终态由网关核心统一负责。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from core.models import (
    AssetDescriptor,
    ErrorObject,
    Usage,
)


@dataclass(frozen=True, slots=True)
class ResolvedAsset:
    """Provider 可读取的已鉴权本地 Asset；不得写入公开响应。"""

    descriptor: AssetDescriptor
    path: Path


class AssetAccess(Protocol):
    """绑定单个租户与主体的 Provider Asset 能力。"""

    def resolve(self, asset_id: str) -> ResolvedAsset: ...

    async def store_output(
        self,
        *,
        filename: str,
        mime_type: str,
        content: bytes | AsyncIterator[bytes],
        metadata: Mapping[str, Any] | None = None,
        checksum_sha256: str | None = None,
    ) -> AssetDescriptor: ...


@dataclass(frozen=True, slots=True)
class RequestContext:
    tenant_id: str
    subject_id: str
    request_id: str
    response_id: str
    trace_id: str
    gateway_system_prompt: str = ""
    live_config_revision: str = "empty"
    gateway_key_id: str | None = None
    assets: AssetAccess | None = None


class ProviderEventKind(StrEnum):
    ITEM_ADDED = "output_item.added"
    TEXT_DELTA = "output_text.delta"
    # Core-synthesized closure for providers whose native stream only exposes
    # deltas.  The public Kemo 2.0 stream requires output_text.done before
    # usage/terminal frames, so the executor may emit this normalized event.
    TEXT_DONE = "output_text.done"
    REFUSAL_DELTA = "output_refusal.delta"
    REFUSAL_DONE = "output_refusal.done"
    AUDIO_DELTA = "output_audio.delta"
    REASONING_SUMMARY_DELTA = "reasoning.summary.delta"
    REASONING_CONTENT_DELTA = "reasoning.content.delta"
    TOOL_ARGUMENTS_DELTA = "tool_call.arguments.delta"
    TOOL_COMPLETED = "tool_call.completed"
    MEDIA_COMPLETED = "output_media.completed"
    USAGE = "usage.updated"
    COMPLETED = "provider.completed"
    INCOMPLETE = "provider.incomplete"
    FAILED = "provider.failed"
    CANCELLED = "provider.cancelled"
    # A stream-level failure that cannot be represented as a complete
    # KemoResponse.  The gateway publishes this as the standalone ``error``
    # terminal event and does not duplicate it into a response.failed frame.
    ERROR = "error"


@dataclass(slots=True)
class ProviderResult:
    """协议适配器转换后的统一结果。"""

    status: str
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider_response_id: str | None = None
    error: ErrorObject | None = None
    incomplete_details: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderChoiceResult:
    """One indexed candidate returned by a multi-choice Provider call."""

    choice_index: int
    result: ProviderResult


@dataclass(slots=True)
class ProviderBatchResult:
    """One upstream execution with ordered candidates and aggregate usage."""

    responses: list[ProviderChoiceResult]
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class ProviderEmbedding:
    """厂商向量结果；index 必须对应请求 inputs 的原始位置。"""

    index: int
    vector: list[float]


@dataclass(slots=True)
class ProviderEmbeddingResult:
    embeddings: list[ProviderEmbedding]
    vector_space_id: str
    # IDs that the provider actually truncated according to its tokenizer.
    # An empty list is distinct from an omitted/unknown report at the contract
    # boundary; providers must populate this when truncate was requested.
    truncated_ids: list[str] = field(default_factory=list)
    truncation_reported: bool = False
    usage: Usage = field(default_factory=Usage)
    model_version: str | None = None
    provider_response_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderRerankItem:
    """厂商排序结果；index 必须对应请求 documents 的原始位置。"""

    index: int
    relevance_score: float


@dataclass(slots=True)
class ProviderRerankResult:
    results: list[ProviderRerankItem]
    usage: Usage = field(default_factory=Usage)
    model_version: str | None = None
    provider_response_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderEvent:
    """无传输信封的标准化厂商事件。"""

    kind: ProviderEventKind
    item_id: str | None = None
    content_index: int | None = None
    call_id: str | None = None
    name: str | None = None
    delta: str | None = None
    text: str | None = None
    item: dict[str, Any] | None = None
    usage: Usage | None = None
    result: ProviderResult | None = None
    error: ErrorObject | None = None
    data: dict[str, Any] | None = None
    provider_response_id: str | None = None


class ProviderException(Exception):
    """协议适配器完成脱敏和错误映射后抛出的异常。"""

    def __init__(self, error: ErrorObject) -> None:
        super().__init__(error.message)
        self.error = error
