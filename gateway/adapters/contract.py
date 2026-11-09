from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import Field

from core.provider_contract import (
    ProviderBatchResult, ProviderEmbeddingResult, ProviderEvent, ProviderResult,
    ProviderRerankResult, RequestContext,
)
from gateway.domain import Document
from gateway.auth import AuthScheme, STANDARD_AUTH


class MediaSupport(Document):
    input: tuple[Literal["text", "image", "audio", "video", "file"], ...] = ("text",)
    output: tuple[Literal["text", "image", "audio", "video", "file", "embedding", "score"], ...] = ("text",)


class AdapterManifest(Document):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    name: str
    version: str = "1.0.0"
    contract_version: str = "1.0"
    operations: tuple[str, ...]
    auth_schemes: tuple[AuthScheme, ...] = STANDARD_AUTH
    streaming: bool = False
    discovery: bool = True
    endpoints: dict[str, str] = Field(default_factory=dict)
    fixed_headers: dict[str, str] = Field(default_factory=dict)
    body_fields: tuple[str, ...] = (
        "model", "messages", "input", "stream", "tools", "tool_choice", "parallel_tool_calls",
        "temperature", "top_p", "top_k", "stop", "seed", "presence_penalty", "frequency_penalty",
        "logit_bias", "n", "max_tokens", "max_completion_tokens", "max_output_tokens",
        "response_format", "reasoning", "reasoning_effort", "thinking", "stream_options",
    )
    media: MediaSupport = Field(default_factory=MediaSupport)
    probe_operation: Literal[
        "image_generation", "audio_transcription", "speech_generation",
        "speech_to_speech", "video_generation", "file_generation",
    ] | None = None
    # These are encoder/decoder limits, never evidence about a particular model.
    tools: bool = True
    multiple_choices: bool = False
    structured_output: bool = False
    reasoning_returns: tuple[str, ...] = ()
    reasoning_levels: tuple[str, ...] = ()
    persisted_reasoning: bool = False
    multimodal_tool_results: bool = False
    prediction: bool = False
    service_tiers: bool = False
    logprobs: bool = False
    verbosity: bool = False
    embedding_input_types: tuple[str, ...] = ("document",)
    embedding_normalization: str = "unknown"
    config_schema: dict = Field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    usage_semantics: str
    builtin: bool = True


class TextGenerationAdapter(Protocol):
    manifest: AdapterManifest
    async def execute(self, request, context: RequestContext, upstream_model: str, upstream, headers) -> ProviderResult | ProviderBatchResult: ...
    def stream(self, request, context: RequestContext, upstream_model: str, upstream, headers) -> AsyncIterator[ProviderEvent]: ...


class EmbeddingAdapter(Protocol):
    manifest: AdapterManifest
    async def embed(self, request, context: RequestContext, upstream_model: str, upstream, headers) -> ProviderEmbeddingResult: ...


class RerankAdapter(Protocol):
    manifest: AdapterManifest
    async def rerank(self, request, context: RequestContext, upstream_model: str, upstream, headers) -> ProviderRerankResult: ...
