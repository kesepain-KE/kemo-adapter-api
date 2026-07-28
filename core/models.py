"""Kemo 网关公开协议的严格模型。

Provider 包不得定义或修改这些公开模型；它只能把厂商协议转换成这些模型。
完整字段以《Kemo网关-统一Provider协议适配要求》为准。
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel, Mapping[str, Any]):
    """严格协议模型，同时保留 Provider 既有的 Mapping 读取方式。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def __getitem__(self, key: str) -> Any:
        fields = type(self).model_fields
        if key in fields:
            return getattr(self, key)
        for name, field_info in fields.items():
            if field_info.alias == key:
                return getattr(self, name)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(
            field_info.alias or name
            for name, field_info in type(self).model_fields.items()
        )

    def __len__(self) -> int:
        return len(type(self).model_fields)


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


class MediaUsage(StrictModel):
    input_images: int | None = Field(default=None, ge=0)
    input_audio_seconds: float | None = Field(default=None, ge=0)
    input_video_seconds: float | None = Field(default=None, ge=0)
    output_audio_seconds: float | None = Field(default=None, ge=0)
    output_images: int | None = Field(default=None, ge=0)
    output_video_seconds: float | None = Field(default=None, ge=0)


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
    media: MediaUsage = Field(default_factory=MediaUsage)
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
    media: MediaUsage = Field(default_factory=MediaUsage)
    stages: list[StageUsage] = Field(default_factory=list)
    provider_raw: dict[str, Any] = Field(default_factory=dict)


class AssetDescriptor(StrictModel):
    protocol_version: Literal["1.0"] = "1.0"
    id: str
    object: Literal["kemo.asset"] = "kemo.asset"
    status: Literal["uploading", "processing", "ready", "failed", "deleted"]
    purpose: Literal["input", "output"]
    filename: str
    mime_type: str
    size: int = Field(ge=0)
    checksum_sha256: str
    created_at: datetime
    expires_at: datetime
    error: ErrorObject | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_asset_descriptor(self) -> "AssetDescriptor":
        if not self.id.startswith("asset_") or not _ID_RE.fullmatch(self.id):
            raise ValueError("Asset id 必须使用 asset_ 前缀且不超过 128 字符")
        checksum = self.checksum_sha256.strip().casefold()
        if not _SHA256_RE.fullmatch(checksum):
            raise ValueError("checksum_sha256 必须是 64 位十六进制 SHA-256")
        self.checksum_sha256 = checksum
        return self


class ReasoningCapabilities(StrictModel):
    supported: bool = False
    efforts: list[str] = Field(default_factory=list)
    summary: bool = False
    persisted_state: bool = False


class ToolCapabilities(StrictModel):
    function_calling: bool = False
    parallel_calls: bool = False
    multimodal_results: bool = False


class MediaSource(StrictModel):
    kind: Literal[
        "object_store",
        "url",
        "data_url",
        "provider_file_id",
        "inline_base64",
    ]
    uri: str | None = None
    provider: str | None = None
    file_id: str | None = None
    data: str | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "MediaSource":
        if self.kind in {"object_store", "url", "data_url"} and not self.uri:
            raise ValueError(f"source.kind={self.kind} 要求 uri")
        if self.kind == "provider_file_id" and not (self.provider and self.file_id):
            raise ValueError("provider_file_id 要求 provider 和 file_id")
        if self.kind == "inline_base64" and not self.data:
            raise ValueError("inline_base64 要求 data")
        return self


class TextContent(StrictModel):
    type: Literal["text"] = "text"
    text: str
    language: str | None = None


class AssetContent(StrictModel):
    asset_id: str | None = None
    source: MediaSource | None = None
    mime_type: str | None = None
    checksum_sha256: str | None = None

    @model_validator(mode="after")
    def validate_asset(self) -> "AssetContent":
        if not self.asset_id and self.source is None:
            raise ValueError("媒体内容至少需要 asset_id 或 source")
        if self.asset_id is not None and not _ID_RE.fullmatch(self.asset_id):
            raise ValueError("asset_id 必须是 1-128 位稳定标识符，不能是本地路径")
        if self.checksum_sha256 is not None:
            checksum = self.checksum_sha256.strip().casefold()
            if not _SHA256_RE.fullmatch(checksum):
                raise ValueError("checksum_sha256 必须是 64 位十六进制 SHA-256")
            self.checksum_sha256 = checksum
        return self


class ImageContent(AssetContent):
    type: Literal["image"] = "image"
    detail: Literal["auto", "low", "high"] = "auto"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class AudioContent(AssetContent):
    type: Literal["audio"] = "audio"
    duration_ms: int | None = Field(default=None, ge=0)
    transcript: str | None = None


class VideoDerived(StrictModel):
    transcript_asset_id: str | None = None
    keyframe_asset_ids: list[str] = Field(default_factory=list)
    timeline_asset_id: str | None = None


class VideoContent(AssetContent):
    type: Literal["video"] = "video"
    duration_ms: int | None = Field(default=None, ge=0)
    derived: VideoDerived | None = None


class FileContent(AssetContent):
    type: Literal["file"] = "file"
    filename: str | None = None


class JsonContent(StrictModel):
    type: Literal["json"] = "json"
    data: Any
    schema_name: str | None = None


class ReferenceContent(StrictModel):
    type: Literal["reference"] = "reference"
    target_id: str
    label: str | None = None


ContentBlock = Annotated[
    TextContent
    | ImageContent
    | AudioContent
    | VideoContent
    | FileContent
    | JsonContent
    | ReferenceContent,
    Field(discriminator="type"),
]


ItemStatus = Literal["in_progress", "completed", "incomplete", "failed"]


class ItemBase(StrictModel):
    id: str
    status: ItemStatus = "completed"
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_id(self) -> "ItemBase":
        if not _ID_RE.fullmatch(self.id):
            raise ValueError("item.id 必须是 1-128 位稳定标识符")
        return self


class MessageItem(ItemBase):
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    phase: Literal["commentary", "final_answer"] | None = None
    content: list[ContentBlock] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_phase(self) -> "MessageItem":
        if self.role == "user" and self.phase is not None:
            raise ValueError("user message 不允许 phase")
        return self


class ProviderState(StrictModel):
    kind: Literal["encrypted", "opaque"]
    data: str
    provider: str
    model: str | None = None
    version: str | None = None
    expires_at: datetime | None = None


class ReasoningItem(ItemBase):
    type: Literal["reasoning"] = "reasoning"
    summary: str | None = None
    content: str | None = None
    provider_state: ProviderState | None = None
    token_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_body(self) -> "ReasoningItem":
        if not (self.summary or self.content or self.provider_state):
            raise ValueError("reasoning 至少需要 summary、content 或 provider_state")
        return self


class ToolCallItem(ItemBase):
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_raw: str | None = None
    parse_error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_tool(self) -> "ToolCallItem":
        if not self.call_id.strip() or not self.name.strip():
            raise ValueError("工具 call_id/name 不能为空")
        return self


class ToolResultItem(ItemBase):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    name: str
    is_error: bool = False
    content: list[ContentBlock] = Field(min_length=1)


Item = Annotated[
    MessageItem | ReasoningItem | ToolCallItem | ToolResultItem,
    Field(discriminator="type"),
]


class ReasoningConfig(StrictModel):
    enabled: bool = False
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "none"
    return_mode: Literal["none", "summary", "content", "auto"] = Field(
        default="none", alias="return"
    )
    context: Literal["none", "current_turn", "all_turns", "auto"] = "auto"


class GenerationConfig(StrictModel):
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    parallel_tool_calls: bool = True


class AudioOutputConfig(StrictModel):
    format: str = "mp3"
    voice: str = "default"


class ImageOutputConfig(StrictModel):
    format: str = "png"
    size: str = "1024x1024"


class VideoOutputConfig(StrictModel):
    format: str = "mp4"
    duration_seconds: float | None = Field(default=None, gt=0)


class FileOutputConfig(StrictModel):
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=127)


class OutputConfig(StrictModel):
    modalities: list[Literal["text", "audio", "image", "video", "file"]] = Field(
        default_factory=lambda: ["text"], min_length=1
    )
    audio: AudioOutputConfig | None = None
    image: ImageOutputConfig | None = None
    video: VideoOutputConfig | None = None
    file: FileOutputConfig | None = None

    @model_validator(mode="after")
    def validate_configs(self) -> "OutputConfig":
        if len(self.modalities) != len(set(self.modalities)):
            raise ValueError("output.modalities 不得重复")
        for modality in ("audio", "image", "video", "file"):
            config = getattr(self, modality)
            requested = modality in self.modalities
            if requested and config is None:
                raise ValueError(f"请求 {modality} 输出时必须提供 output.{modality}")
            if not requested and config is not None:
                raise ValueError(f"output.{modality} 只能在 modalities 包含 {modality} 时提供")
        return self


class ToolDefinition(StrictModel):
    type: Literal["function"] = "function"
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True
    permission: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)


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
        if self.task == "llm":
            self._validate_multimodal_operations()
        return self

    def _validate_multimodal_operations(self) -> None:
        operations = self.extensions.get("operations")
        if operations is None:
            return
        if not isinstance(operations, Mapping):
            raise ValueError("extensions.operations 必须是对象")
        requirements = {
            "conversation": (set(), {"text"}),
            "vision": ({"text", "image"}, {"text"}),
            "image_generation": ({"text"}, {"image"}),
            "image_edit": ({"text", "image"}, {"image"}),
            "audio_transcription": ({"audio"}, {"text"}),
            "speech_generation": ({"text"}, {"audio"}),
            "speech_to_speech": ({"audio"}, {"audio"}),
            "video_understanding": ({"video"}, {"text"}),
            "video_generation": ({"text"}, {"video"}),
        }
        for name, declaration in operations.items():
            if isinstance(declaration, bool):
                supported = declaration
            elif isinstance(declaration, Mapping):
                supported = declaration.get("supported") is True
                if "supported" not in declaration or not isinstance(
                    declaration.get("supported"), bool
                ):
                    raise ValueError(
                        f"extensions.operations.{name}.supported 必须是布尔值"
                    )
            else:
                raise ValueError(
                    f"extensions.operations.{name} 必须是布尔值或对象"
                )
            if not supported or name not in requirements:
                continue
            required_inputs, required_outputs = requirements[name]
            missing_inputs = required_inputs - set(self.input_modalities)
            missing_outputs = required_outputs - set(self.output_modalities)
            if missing_inputs or missing_outputs:
                raise ValueError(
                    f"操作 {name} 与 input_modalities/output_modalities 声明不一致"
                )


class ModelCatalogItem(StrictModel):
    id: str
    object: Literal["kemo.model"] = "kemo.model"
    provider_id: str
    provider_model: str
    task: Literal["llm", "embedding", "rerank", "unknown"]
    capabilities_available: bool
    capabilities_url: str


class ModelCatalogResponse(StrictModel):
    protocol_version: Literal["1.0"] = "1.0"
    object: Literal["kemo.model_list"] = "kemo.model_list"
    count: int = Field(ge=0)
    data: list[ModelCatalogItem]


class CompatibleModelItem(StrictModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default=0, ge=0)
    owned_by: str


class CompatibleModelList(StrictModel):
    object: Literal["list"] = "list"
    data: list[CompatibleModelItem]


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
    reasoning: ReasoningConfig | None = None
    generation: GenerationConfig
    output: OutputConfig
    tools: list[ToolDefinition]
    input: list[Item]
    provider_options: dict[str, Any]
    metadata: dict[str, Any]
    extensions: dict[str, Any]

    @model_validator(mode="after")
    def validate_items(self) -> "KemoRequest":
        item_ids: set[str] = set()
        calls: dict[str, str] = {}
        completed_results: set[str] = set()
        for index, item in enumerate(self.input):
            if item.id in item_ids:
                raise ValueError(f"input[{index}].id 重复：{item.id}")
            item_ids.add(item.id)
            if isinstance(item, ToolCallItem):
                if item.call_id in calls:
                    raise ValueError(f"tool_call.call_id 重复：{item.call_id}")
                calls[item.call_id] = item.name
            elif isinstance(item, ToolResultItem):
                expected_name = calls.get(item.call_id)
                if expected_name is None:
                    raise ValueError(f"tool_result 无匹配 tool_call：{item.call_id}")
                if expected_name != item.name:
                    raise ValueError(f"tool_result.name 与 tool_call 不一致：{item.call_id}")
                if item.call_id in completed_results:
                    raise ValueError(f"tool_result.call_id 重复：{item.call_id}")
                completed_results.add(item.call_id)
        return self


ResponseStatus = Literal["completed", "requires_action", "incomplete", "failed", "cancelled"]


class KemoResponse(StrictModel):
    protocol_version: str = "1.0"
    id: str
    request_id: str
    object: Literal["kemo.response"] = "kemo.response"
    status: ResponseStatus
    model: str
    output: list[Item] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    error: ErrorObject | None = None
    incomplete_details: dict[str, Any] | None = None
    provider_response_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_status(self) -> "KemoResponse":
        if self.status == "failed" and self.error is None:
            raise ValueError("failed response 必须包含 error")
        if self.status == "requires_action" and not any(
            isinstance(item, ToolCallItem) for item in self.output
        ):
            raise ValueError("requires_action response 必须包含 tool_call")
        if self.status == "incomplete" and self.incomplete_details is None:
            raise ValueError("incomplete response 必须包含 incomplete_details")
        ids = [item.id for item in self.output]
        if len(ids) != len(set(ids)):
            raise ValueError("response.output item id 不得重复")
        if self.status == "completed" and not self.output:
            raise ValueError("completed response 必须包含完成结果")
        for item in self.output:
            if isinstance(item, MessageItem):
                if item.role != "assistant":
                    raise ValueError("response.output message 必须使用 assistant role")
                _validate_output_media_item(item)
        return self


SSEEventType = Literal[
    "response.created",
    "output_item.added",
    "reasoning.summary.delta",
    "reasoning.content.delta",
    "tool_call.arguments.delta",
    "tool_call.completed",
    "output_text.delta",
    "output_audio.delta",
    "output_media.completed",
    "usage.updated",
    "response.completed",
    "response.incomplete",
    "response.failed",
    "response.cancelled",
    "error",
]


class SSEEvent(StrictModel):
    type: SSEEventType
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
    item: Item | None = None
    usage: Usage | None = None
    response: KemoResponse | None = None
    error: ErrorObject | None = None
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_event_payload(self) -> "SSEEvent":
        if self.type in {
            "output_text.delta",
            "output_audio.delta",
            "reasoning.summary.delta",
            "reasoning.content.delta",
        } and (self.item_id is None or self.delta is None):
            raise ValueError(f"{self.type} 必须包含 item_id 和 delta")
        if self.type in {"output_text.delta", "output_audio.delta"} and (
            self.content_index is None
        ):
            raise ValueError(f"{self.type} 必须包含 content_index")
        if self.type == "output_audio.delta" and self.delta is not None:
            try:
                decoded = base64.b64decode(self.delta, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("output_audio.delta 必须是有效 Base64") from exc
            if not decoded:
                raise ValueError("output_audio.delta 不能是空音频片段")
        if self.type == "tool_call.arguments.delta" and not all(
            (self.item_id, self.call_id, self.name, self.delta is not None)
        ):
            raise ValueError(
                "tool_call.arguments.delta 必须包含 item_id/call_id/name/delta"
            )
        if self.type == "tool_call.completed":
            if not isinstance(self.item, ToolCallItem):
                raise ValueError("tool_call.completed 必须包含完整 ToolCallItem")
            if self.call_id is not None and self.item.call_id != self.call_id:
                raise ValueError("tool_call.completed 的 call_id 与 item 不一致")
        if self.type == "output_media.completed":
            if not isinstance(self.item, MessageItem) or self.item.role != "assistant":
                raise ValueError(
                    "output_media.completed 必须包含 assistant MessageItem"
                )
            if self.item_id != self.item.id:
                raise ValueError("output_media.completed 的 item_id 与 item 不一致")
            _validate_output_media_item(self.item, require_media=True)
        if self.type == "usage.updated" and self.usage is None:
            raise ValueError("usage.updated 必须包含 usage")

        terminal_statuses = {
            "response.completed": {"completed", "requires_action"},
            "response.incomplete": {"incomplete"},
            "response.failed": {"failed"},
            "response.cancelled": {"cancelled"},
        }
        expected = terminal_statuses.get(self.type)
        if expected is not None and (
            self.response is None or self.response.status not in expected
        ):
            raise ValueError(f"{self.type} 必须包含匹配状态的完整 KemoResponse")
        if self.type == "error" and self.error is None:
            raise ValueError("error 事件必须包含统一 error")
        return self


def _validate_output_media_item(
    item: MessageItem, *, require_media: bool = False
) -> None:
    media_blocks = [
        block
        for block in item.content
        if isinstance(block, (ImageContent, AudioContent, VideoContent, FileContent))
    ]
    if require_media and not media_blocks:
        raise ValueError("媒体完成事件必须至少包含一个媒体 Content Block")
    for block in media_blocks:
        if not block.asset_id:
            raise ValueError("响应媒体必须包含可下载的 asset_id")
        if not block.mime_type:
            raise ValueError("响应媒体必须包含真实 mime_type")
        if not block.checksum_sha256:
            raise ValueError("响应媒体必须包含 checksum_sha256")
