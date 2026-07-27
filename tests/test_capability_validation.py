from __future__ import annotations

from typing import Any

import pytest

from core.capability_validation import validate_llm_request_capabilities
from core.models import (
    EmbeddingCapabilities,
    KemoRequest,
    ModelCapabilities,
    ReasoningCapabilities,
    ToolCapabilities,
)
from core.provider_contract import ProviderException


def request(
    *,
    content: list[dict[str, Any]] | None = None,
    output_modalities: list[str] | None = None,
    stream: bool = False,
    tools: list[dict[str, Any]] | None = None,
    reasoning: dict[str, Any] | None = None,
) -> KemoRequest:
    return KemoRequest(
        protocol_version="1.0",
        request_id="capability-test",
        attempt=1,
        model="fake-model",
        stream=stream,
        system_prompt="",
        reasoning=reasoning,
        generation={"max_output_tokens": 64, "parallel_tool_calls": False},
        output={"modalities": output_modalities or ["text"]},
        tools=tools or [],
        input=[
            {
                "type": "message",
                "role": "user",
                "content": content or [{"type": "text", "text": "hello"}],
            }
        ],
        provider_options={},
        metadata={},
        extensions={},
    )


def capabilities(
    *,
    task: str = "llm",
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
    streaming: bool = True,
    reasoning: ReasoningCapabilities | None = None,
    tools: ToolCapabilities | None = None,
) -> ModelCapabilities:
    return ModelCapabilities(
        model="fake-model",
        task=task,
        input_modalities=input_modalities or ["text"],
        output_modalities=output_modalities or ["text"],
        streaming=streaming,
        reasoning=reasoning or ReasoningCapabilities(),
        tools=tools or ToolCapabilities(),
    )


def test_allows_declared_image_input() -> None:
    validate_llm_request_capabilities(
        request(
            content=[
                {"type": "text", "text": "describe"},
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "source": {"kind": "inline_base64", "data": "YWJj"},
                },
            ]
        ),
        capabilities(input_modalities=["text", "image"]),
    )


@pytest.mark.parametrize(
    ("req", "caps", "code"),
    [
        (
            request(content=[{"type": "image", "source": {"kind": "url", "uri": "https://example.test/a.png"}}]),
            capabilities(),
            "UNSUPPORTED_INPUT_MODALITY",
        ),
        (
            request(output_modalities=["audio"]),
            capabilities(),
            "UNSUPPORTED_OUTPUT_MODALITY",
        ),
        (request(stream=True), capabilities(streaming=False), "STREAMING_UNSUPPORTED"),
        (
            request(tools=[{"type": "function", "name": "lookup"}]),
            capabilities(),
            "TOOLS_UNSUPPORTED",
        ),
        (
            request(reasoning={"enabled": True, "effort": "high"}),
            capabilities(),
            "REASONING_UNSUPPORTED",
        ),
    ],
)
def test_rejects_requests_outside_declared_capabilities(
    req: KemoRequest,
    caps: ModelCapabilities,
    code: str,
) -> None:
    with pytest.raises(ProviderException) as captured:
        validate_llm_request_capabilities(req, caps)
    assert captured.value.error.code == code


def test_rejects_non_llm_task() -> None:
    caps = ModelCapabilities(
        model="fake-model",
        task="embedding",
        input_modalities=["text"],
        output_modalities=["embedding"],
        streaming=False,
        embedding=EmbeddingCapabilities(
            input_types=["query", "document"],
            default_dimensions=3,
            max_batch_size=8,
        ),
    )
    with pytest.raises(ProviderException) as captured:
        validate_llm_request_capabilities(request(), caps)
    assert captured.value.error.code == "MODEL_TASK_MISMATCH"
