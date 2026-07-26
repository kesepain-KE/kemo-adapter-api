"""Kemo 网关公开协议的最小严格模型。

Provider 包不得定义或修改这些公开模型；它只能把厂商协议转换成这些模型。
完整字段以《Kemo网关-统一Provider协议适配要求》为准，当前文件先提供骨架运行所需部分。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorObject(StrictModel):
    type: str
    code: str
    message: str
    retryable: bool = False
    retry_after_ms: int | None = None
    provider_status: int | None = None
    provider_request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class UsageMeasurement(StrictModel):
    mode: Literal["provider", "gateway", "estimated", "mixed", "unknown"] = "unknown"
    exact: bool = False
    exact_fields: list[str] = Field(default_factory=list)
    estimated_fields: list[str] = Field(default_factory=list)


class StageUsage(StrictModel):
    stage: str
    provider: str
    model: str
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    measurement: UsageMeasurement = Field(default_factory=UsageMeasurement)
    media: dict[str, int | float | None] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


class Usage(StrictModel):
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    visible_output_tokens: int | None = None
    total_tokens: int | None = None
    measurement: UsageMeasurement = Field(default_factory=UsageMeasurement)
    media: dict[str, int | float | None] = Field(default_factory=dict)
    stages: list[StageUsage] = Field(default_factory=list)
    provider_raw: dict[str, Any] = Field(default_factory=dict)


class ReasoningCapabilities(StrictModel):
    supported: bool = False
    efforts: list[str] = Field(default_factory=list)
    summary: bool = False
    persisted_state: bool = False


class ToolCapabilities(StrictModel):
    function_calling: bool = False
    parallel_calls: bool = False
    multimodal_results: bool = False


class EmbeddingCapabilities(StrictModel):
    input_types: list[Literal["query", "document"]]
    default_dimensions: int = Field(gt=0)
    supported_dimensions: list[int] = Field(default_factory=list)
    max_batch_size: int = Field(gt=0)
    max_input_tokens_per_item: int | None = Field(default=None, gt=0)
    normalization: Literal["always", "optional", "never", "unknown"] = "unknown"


class RerankCapabilities(StrictModel):
    max_documents: int = Field(gt=0)
    max_query_tokens: int | None = Field(default=None, gt=0)
    max_document_tokens: int | None = Field(default=None, gt=0)
    supports_return_documents: bool = True
    score_semantics: Literal["higher_is_more_relevant"] = "higher_is_more_relevant"


class ModelCapabilities(StrictModel):
    model: str
    task: Literal["llm", "embedding", "rerank"] = "llm"
    input_modalities: list[str]
    output_modalities: list[str]
    streaming: bool
    reasoning: ReasoningCapabilities = Field(default_factory=ReasoningCapabilities)
    tools: ToolCapabilities = Field(default_factory=ToolCapabilities)
    structured_output: bool = False
    embedding: EmbeddingCapabilities | None = None
    rerank: RerankCapabilities | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_task_capabilities(self) -> "ModelCapabilities":
        if self.task == "embedding" and self.embedding is None:
            raise ValueError("embedding 模型必须声明 embedding capabilities")
        if self.task == "rerank" and self.rerank is None:
            raise ValueError("rerank 模型必须声明 rerank capabilities")
        if self.task != "embedding" and self.embedding is not None:
            raise ValueError("非 embedding 模型不能声明 embedding capabilities")
        if self.task != "rerank" and self.rerank is not None:
            raise ValueError("非 rerank 模型不能声明 rerank capabilities")
        return self


class EmbeddingInput(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=2_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingRequest(StrictModel):
    protocol_version: Literal["1.0"]
    request_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1)
    input_type: Literal["query", "document"]
    inputs: list[EmbeddingInput] = Field(min_length=1, max_length=2048)
    dimensions: int | None = Field(default=None, gt=0)
    normalize: bool | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "EmbeddingRequest":
        ids = [item.id for item in self.inputs]
        if len(ids) != len(set(ids)):
            raise ValueError("embedding input id 必须唯一")
        return self


class EmbeddingData(StrictModel):
    id: str
    index: int = Field(ge=0)
    vector: list[float]


class EmbeddingResponse(StrictModel):
    protocol_version: Literal["1.0"] = "1.0"
    object: Literal["kemo.embedding_list"] = "kemo.embedding_list"
    request_id: str
    model: str
    model_version: str | None = None
    vector_space_id: str
    dimensions: int = Field(gt=0)
    data: list[EmbeddingData]
    usage: Usage = Field(default_factory=Usage)
    provider_response_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


class RerankDocument(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=2_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RerankRequest(StrictModel):
    protocol_version: Literal["1.0"]
    request_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=2_000_000)
    documents: list[RerankDocument] = Field(min_length=1, max_length=4096)
    top_n: int | None = Field(default=None, gt=0)
    return_documents: bool = False
    provider_options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_documents(self) -> "RerankRequest":
        ids = [item.id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("rerank document id 必须唯一")
        if self.top_n is not None and self.top_n > len(self.documents):
            raise ValueError("top_n 不能超过 documents 数量")
        return self


class RerankResultItem(StrictModel):
    rank: int = Field(ge=1)
    document_id: str
    index: int = Field(ge=0)
    relevance_score: float
    document: RerankDocument | None = None


class RerankResponse(StrictModel):
    protocol_version: Literal["1.0"] = "1.0"
    object: Literal["kemo.rerank"] = "kemo.rerank"
    request_id: str
    model: str
    model_version: str | None = None
    score_semantics: Literal["higher_is_more_relevant"] = "higher_is_more_relevant"
    results: list[RerankResultItem]
    usage: Usage = Field(default_factory=Usage)
    provider_response_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


class KemoRequest(StrictModel):
    protocol_version: str
    request_id: str = Field(min_length=1, max_length=128)
    parent_request_id: str | None = None
    attempt: int = Field(ge=1)
    model: str
    stream: bool
    system_prompt: str
    reasoning: dict[str, Any] | None = None
    generation: dict[str, Any]
    output: dict[str, Any]
    tools: list[dict[str, Any]]
    input: list[dict[str, Any]]
    provider_options: dict[str, Any]
    metadata: dict[str, Any]
    extensions: dict[str, Any]


ResponseStatus = Literal["completed", "requires_action", "incomplete", "failed", "cancelled"]


class KemoResponse(StrictModel):
    protocol_version: str = "1.0"
    id: str
    request_id: str
    object: Literal["kemo.response"] = "kemo.response"
    status: ResponseStatus
    model: str
    output: list[dict[str, Any]] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    error: ErrorObject | None = None
    incomplete_details: dict[str, Any] | None = None
    provider_response_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


class SSEEvent(StrictModel):
    type: str
    event_id: str
    sequence: int = Field(ge=0)
    request_id: str
    response_id: str
    timestamp: str
    item_id: str | None = None
    content_index: int | None = None
    call_id: str | None = None
    name: str | None = None
    delta: str | None = None
    item: dict[str, Any] | None = None
    usage: Usage | None = None
    response: KemoResponse | None = None
    error: ErrorObject | None = None
    data: dict[str, Any] | None = None
