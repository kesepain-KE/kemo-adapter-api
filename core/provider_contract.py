"""所有厂商包统一暴露给网关的唯一契约。

关键边界：Provider 包输出标准化事件，而不是 SSE 字节或带 sequence 的 Kemo 事件。
sequence、event_id、重放和唯一终态由网关核心统一负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.models import (
    EmbeddingRequest,
    ErrorObject,
    KemoRequest,
    ModelCapabilities,
    RerankRequest,
    Usage,
)


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


class ProviderEventKind(StrEnum):
    ITEM_ADDED = "output_item.added"
    TEXT_DELTA = "output_text.delta"
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


@dataclass(slots=True)
class ProviderResult:
    """厂商包完成协议转换后的统一结果。"""

    status: str
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider_response_id: str | None = None
    error: ErrorObject | None = None
    incomplete_details: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderEmbedding:
    """厂商向量结果；index 必须对应请求 inputs 的原始位置。"""

    index: int
    vector: list[float]


@dataclass(slots=True)
class ProviderEmbeddingResult:
    embeddings: list[ProviderEmbedding]
    vector_space_id: str
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
    item: dict[str, Any] | None = None
    usage: Usage | None = None
    result: ProviderResult | None = None
    error: ErrorObject | None = None
    data: dict[str, Any] | None = None
    provider_response_id: str | None = None


class ProviderException(Exception):
    """厂商包完成脱敏和错误映射后抛出的异常。"""

    def __init__(self, error: ErrorObject) -> None:
        super().__init__(error.message)
        self.error = error


class ProviderPackage(ABC):
    """一个厂商目录对网关暴露的 Facade。"""

    provider_id: str

    @property
    @abstractmethod
    def models(self) -> frozenset[str]:
        """该包接受的完整网关模型名，例如 ``openai-gpt-5``。"""

    @abstractmethod
    async def capabilities(self, model: str) -> ModelCapabilities:
        """返回该厂商包确认过的真实能力。"""

    async def execute(self, request: KemoRequest, context: RequestContext) -> ProviderResult:
        """完成一次 LLM 非流式执行；非 LLM 包保持默认实现。"""
        del request, context
        raise LookupError(f"Provider {self.provider_id} 不支持 LLM response")

    def stream(
        self, request: KemoRequest, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        """执行 LLM 流；非 LLM 包保持默认实现。"""
        del request, context
        raise LookupError(f"Provider {self.provider_id} 不支持 LLM stream")

    async def embed(
        self, request: EmbeddingRequest, context: RequestContext
    ) -> ProviderEmbeddingResult:
        """执行向量化；不支持该任务的包保持默认实现。"""
        del request, context
        raise LookupError(f"Provider {self.provider_id} 不支持 embedding")

    async def rerank(
        self, request: RerankRequest, context: RequestContext
    ) -> ProviderRerankResult:
        """执行重排序；不支持该任务的包保持默认实现。"""
        del request, context
        raise LookupError(f"Provider {self.provider_id} 不支持 rerank")

    async def cancel(self, provider_response_id: str | None, context: RequestContext) -> None:
        """取消厂商任务。同步完成型厂商可以保持默认实现。"""
        return None

    async def reload_config(self, settings: Mapping[str, Any]) -> None:
        """热更新厂商 API 配置；实现必须原子切换且不得中断已有请求。"""
        return None

    async def close(self) -> None:
        """释放连接池等厂商私有资源。"""
        return None

    def diagnostics(self) -> Mapping[str, Any]:
        """只允许返回脱敏且低基数的诊断信息。"""
        return {"provider_id": self.provider_id, "models": sorted(self.models)}
