from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from core.capability_validation import (
    validate_llm_request_capabilities,
    validate_media_url_networks,
)
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
    modalities = output_modalities or ["text"]
    output: dict[str, object] = {"modalities": modalities}
    if "audio" in modalities:
        output["audio"] = {"format": "mp3", "voice": "default"}
    if "image" in modalities:
        output["image"] = {"format": "png", "size": "1024x1024"}
    if "video" in modalities:
        output["video"] = {"format": "mp4"}
    return KemoRequest(
        protocol_version="1.0",
        request_id="capability-test",
        attempt=1,
        model="fake-model",
        stream=stream,
        system_prompt="",
        reasoning=reasoning,
        generation={"max_output_tokens": 64, "parallel_tool_calls": False},
        output=output,
        tools=tools or [],
        input=[
            {
                "id": "msg_user_1",
                "type": "message",
                "role": "user",
                "status": "completed",
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
                    "source": {
                        "kind": "inline_base64",
                        "data": (
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                            "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                        ),
                    },
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
        request(
            tools=[
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        ),
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


def test_reasoning_effort_must_match_model_declaration() -> None:
    declared = capabilities(
        reasoning=ReasoningCapabilities(
            supported=True,
            efforts=["low", "medium"],
        )
    )

    validate_llm_request_capabilities(
        request(reasoning={"enabled": True, "effort": "medium"}),
        declared,
    )

    with pytest.raises(ProviderException) as captured:
        validate_llm_request_capabilities(
            request(reasoning={"enabled": True, "effort": "high"}),
            declared,
        )
    assert captured.value.error.code == "REASONING_EFFORT_UNSUPPORTED"


def test_capabilities_reject_supported_operation_without_required_modalities() -> None:
    with pytest.raises(ValueError, match="声明不一致"):
        ModelCapabilities(
            model="fake-model",
            input_modalities=["text"],
            output_modalities=["text"],
            streaming=True,
            extensions={"operations": {"vision": {"supported": True}}},
        )


def test_external_media_dns_cannot_resolve_to_private_address(monkeypatch) -> None:
    def private_address(*args, **kwargs):
        del args, kwargs
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", private_address)
    req = request(
        content=[
            {"type": "text", "text": "describe"},
            {
                "type": "image",
                "mime_type": "image/png",
                "source": {
                    "kind": "url",
                    "uri": "https://media.example.test/image.png",
                },
            },
        ]
    )
    with pytest.raises(ProviderException) as captured:
        asyncio.run(validate_media_url_networks(req))
    assert captured.value.error.code == "INVALID_MEDIA"
