from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.models import MediaUsage, ModelCapabilities, ToolCapabilities, Usage
from core.provider_contract import (
    ProviderEvent,
    ProviderEventKind,
    ProviderPackage,
    ProviderResult,
    RequestContext,
)
from tests.test_live_config import project


PNG = b"\x89PNG\r\n\x1a\n" + b"framework-image"
WAV = b"RIFF" + b"\x10\x00\x00\x00" + b"WAVE" + b"framework-audio"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"framework-video"
PDF = b"%PDF-1.7\n% framework-file\n"
MODEL = "contract-multimodal"


class ContractMultimodalProvider(ProviderPackage):
    provider_id = "contract"

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def models(self) -> frozenset[str]:
        return frozenset({MODEL})

    async def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            model=model,
            input_modalities=["text", "image", "audio", "video", "file"],
            output_modalities=["text", "image", "audio", "video", "file"],
            streaming=True,
            tools=ToolCapabilities(
                function_calling=True,
                parallel_calls=True,
                multimodal_results=True,
            ),
            extensions={
                "operations": {
                    name: {"supported": True}
                    for name in (
                        "conversation",
                        "vision",
                        "image_generation",
                        "image_edit",
                        "audio_transcription",
                        "speech_generation",
                        "speech_to_speech",
                        "video_understanding",
                        "video_generation",
                    )
                }
            },
        )

    async def execute(self, request, context: RequestContext) -> ProviderResult:
        capability = str(request.metadata.get("capability") or "conversation")
        self.calls.append(capability)
        for item in request.input:
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                asset_id = str(block.get("asset_id") or "")
                if asset_id:
                    assert context.assets is not None
                    resolved = context.assets.resolve(asset_id)
                    assert resolved.path.is_file()
                    assert resolved.descriptor.status == "ready"

        output_media = {
            "image_generation": ("image", "image/png", "generated.png", PNG),
            "image_edit": ("image", "image/png", "edited.png", PNG),
            "speech_generation": ("audio", "audio/wav", "speech.wav", WAV),
            "speech_to_speech": ("audio", "audio/wav", "converted.wav", WAV),
            "video_generation": ("video", "video/mp4", "generated.mp4", MP4),
        }.get(capability)
        if capability == "conversation" and "file" in request.output.modalities:
            output_media = ("file", "application/pdf", "generated.pdf", PDF)
        if output_media is not None:
            kind, mime_type, filename, body = output_media
            assert context.assets is not None
            descriptor = await context.assets.store_output(
                filename=filename,
                mime_type=mime_type,
                content=body,
                metadata={"request_id": request.request_id, "capability": capability},
            )
            usage_field = {
                "image": {"output_images": 1},
                "audio": {"output_audio_seconds": 1.0},
                "video": {"output_video_seconds": 1.0},
                "file": {},
            }[kind]
            content_blocks: list[dict[str, Any]] = [
                {
                    "type": kind,
                    "asset_id": descriptor.id,
                    "mime_type": descriptor.mime_type,
                    "checksum_sha256": descriptor.checksum_sha256,
                }
            ]
            if kind == "file":
                content_blocks.insert(0, {"type": "text", "text": "file generated"})
            return ProviderResult(
                status="completed",
                output=[
                    {
                        "id": f"msg_{request.request_id}",
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "status": "completed",
                        "content": content_blocks,
                    }
                ],
                usage=Usage(media=MediaUsage(**usage_field)),
            )

        text = {
            "vision": "image understood",
            "audio_transcription": "audio transcribed",
            "video_understanding": "video understood",
        }.get(capability, "ok")
        return ProviderResult(
            status="completed",
            output=[
                {
                    "id": f"msg_{request.request_id}",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "status": "completed",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        )

    async def _stream(self, request, context: RequestContext) -> AsyncIterator[ProviderEvent]:
        result = await self.execute(request, context)
        media_item = result.output[0]
        yield ProviderEvent(
            kind=ProviderEventKind.MEDIA_COMPLETED,
            item_id=str(media_item["id"]),
            content_index=0,
            item=media_item,
        )
        yield ProviderEvent(kind=ProviderEventKind.USAGE, usage=result.usage)
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=result)

    def stream(self, request, context: RequestContext) -> AsyncIterator[ProviderEvent]:
        return self._stream(request, context)


def _settings() -> Settings:
    return Settings(
        api_keys={
            "kemo-token": PrincipalConfig(
                "tenant-a",
                "agent-a",
                frozenset({"model:invoke", "asset:read", "asset:write"}),
            )
        }
    )


def _headers(*, request_id: str | None = None) -> dict[str, str]:
    result = {
        "Authorization": "Bearer kemo-token",
        "X-Kemo-Protocol-Version": "1.0",
    }
    if request_id is not None:
        result["Idempotency-Key"] = request_id
    return result


def _app(tmp_path: Path):
    root = project(tmp_path)
    app = create_app(
        _settings(),
        live_config_root=root,
        statistics_root=root / "storage",
        asset_root=root / "storage" / "assets",
        discover_providers=False,
    )
    provider = ContractMultimodalProvider()
    app.state.registry.register(provider)
    return app, provider


def _upload(
    client: TestClient,
    *,
    name: str,
    mime_type: str,
    body: bytes,
    capability: str,
) -> dict[str, Any]:
    checksum = hashlib.sha256(body).hexdigest()
    response = client.post(
        "/assets",
        headers={
            **_headers(),
            "Idempotency-Key": f"asset-{capability}",
            "X-Content-SHA256": checksum,
        },
        data={
            "metadata": json.dumps(
                {"purpose": "input", "capability": capability, "session_id": "s1"}
            )
        },
        files={"file": (name, body, mime_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _request(
    *,
    request_id: str,
    capability: str,
    input_blocks: list[dict[str, Any]],
    output_modality: str,
    asset_id: str | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    output: dict[str, Any] = {"modalities": [output_modality]}
    if output_modality == "image":
        output["image"] = {"format": "png", "size": "1024x1024"}
    elif output_modality == "audio":
        output["audio"] = {"format": "wav", "voice": "default"}
    elif output_modality == "video":
        output["video"] = {"format": "mp4", "duration_seconds": 1}
    metadata: dict[str, Any] = {"capability": capability, "session_id": "s1"}
    if asset_id is not None:
        metadata["multimodal"] = {
            "assets": [{"asset_id": asset_id, "role": "source"}]
        }
    return {
        "protocol_version": "1.0",
        "request_id": request_id,
        "attempt": 1,
        "model": MODEL,
        "stream": stream,
        "system_prompt": "",
        "reasoning": None,
        "generation": {"max_output_tokens": 10000},
        "output": output,
        "tools": [],
        "input": [
            {
                "id": f"msg_{request_id}",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": input_blocks,
            }
        ],
        "provider_options": {},
        "metadata": metadata,
        "extensions": {},
    }


@pytest.mark.parametrize(
    ("capability", "name", "mime_type", "body", "kind"),
    [
        ("vision", "image.png", "image/png", PNG, "image"),
        ("audio_transcription", "audio.wav", "audio/wav", WAV, "audio"),
        ("video_understanding", "video.mp4", "video/mp4", MP4, "video"),
    ],
)
def test_framework_accepts_image_audio_and_video_assets(
    tmp_path: Path,
    capability: str,
    name: str,
    mime_type: str,
    body: bytes,
    kind: str,
) -> None:
    app, provider = _app(tmp_path)
    with TestClient(app) as client:
        asset = _upload(
            client,
            name=name,
            mime_type=mime_type,
            body=body,
            capability=capability,
        )
        request_id = f"req-{kind}-input"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability=capability,
                input_blocks=[
                    {"type": "text", "text": "analyze"},
                    {
                        "type": kind,
                        "asset_id": asset["id"],
                        "mime_type": mime_type,
                        "checksum_sha256": asset["checksum_sha256"],
                    },
                ],
                output_modality="text",
                asset_id=asset["id"],
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert provider.calls == [capability]


def test_framework_accepts_file_asset_without_fake_file_mime_prefix(
    tmp_path: Path,
) -> None:
    app, provider = _app(tmp_path)
    with TestClient(app) as client:
        asset = _upload(
            client,
            name="document.pdf",
            mime_type="application/pdf",
            body=PDF,
            capability="conversation",
        )
        request_id = "req-file-input"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="conversation",
                input_blocks=[
                    {"type": "text", "text": "summarize"},
                    {
                        "type": "file",
                        "asset_id": asset["id"],
                        "filename": "document.pdf",
                        "mime_type": "application/pdf",
                        "checksum_sha256": asset["checksum_sha256"],
                    },
                ],
                output_modality="text",
                asset_id=asset["id"],
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        assert provider.calls == ["conversation"]


@pytest.mark.parametrize(
    ("capability", "kind", "mime_type", "expected"),
    [
        ("image_generation", "image", "image/png", PNG),
        ("speech_generation", "audio", "audio/wav", WAV),
        ("video_generation", "video", "video/mp4", MP4),
    ],
)
def test_framework_persists_image_audio_and_video_outputs(
    tmp_path: Path,
    capability: str,
    kind: str,
    mime_type: str,
    expected: bytes,
) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        request_id = f"req-{kind}-output"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability=capability,
                input_blocks=[{"type": "text", "text": "generate"}],
                output_modality=kind,
            ),
        )
        assert response.status_code == 200, response.text
        content = response.json()["output"][0]["content"][0]
        assert content["type"] == kind
        assert content["mime_type"] == mime_type
        assert content["asset_id"].startswith("asset_")
        downloaded = client.get(
            f"/assets/{content['asset_id']}/content", headers=_headers()
        )
        assert downloaded.status_code == 200
        assert downloaded.content == expected


def test_framework_persists_file_output_as_asset(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        request_id = "req-file-output"
        payload = _request(
            request_id=request_id,
            capability="conversation",
            input_blocks=[{"type": "text", "text": "generate a file"}],
            output_modality="text",
        )
        payload["output"] = {
            "modalities": ["text", "file"],
            "file": {
                "filename": "generated.pdf",
                "mime_type": "application/pdf",
            },
        }
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=payload,
        )
        assert response.status_code == 200, response.text
        content = response.json()["output"][0]["content"]
        file_block = next(block for block in content if block["type"] == "file")
        downloaded = client.get(
            f"/assets/{file_block['asset_id']}/content", headers=_headers()
        )
        assert downloaded.status_code == 200
        assert downloaded.content == PDF


def test_streaming_media_has_completed_event_before_terminal(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        request_id = "req-stream-image"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="image_generation",
                input_blocks=[{"type": "text", "text": "generate"}],
                output_modality="image",
                stream=True,
            ),
        )
        assert response.status_code == 200, response.text
        events = [
            line[7:]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert events == [
            "response.created",
            "output_media.completed",
            "usage.updated",
            "response.completed",
        ]
        assert response.text.index("output_media.completed") < response.text.index(
            "response.completed"
        )


def test_framework_supports_bounded_audio_delta_before_asset_completion(
    tmp_path: Path,
) -> None:
    app, provider = _app(tmp_path)

    async def audio_stream(
        request, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            kind=ProviderEventKind.AUDIO_DELTA,
            item_id="msg_audio_stream",
            content_index=0,
            delta=base64.b64encode(b"audio-preview").decode("ascii"),
        )
        result = await provider.execute(request, context)
        media_item = result.output[0]
        yield ProviderEvent(
            kind=ProviderEventKind.MEDIA_COMPLETED,
            item_id=str(media_item["id"]),
            content_index=0,
            item=media_item,
        )
        yield ProviderEvent(kind=ProviderEventKind.USAGE, usage=result.usage)
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=result)

    provider.stream = audio_stream  # type: ignore[method-assign]
    with TestClient(app) as client:
        request_id = "req-stream-audio"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="speech_generation",
                input_blocks=[{"type": "text", "text": "speak"}],
                output_modality="audio",
                stream=True,
            ),
        )
        assert response.status_code == 200, response.text
        events = [
            line[7:]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert events == [
            "response.created",
            "output_audio.delta",
            "output_media.completed",
            "usage.updated",
            "response.completed",
        ]


def test_framework_rejects_undeclared_multimodal_operation_before_provider(
    tmp_path: Path,
) -> None:
    app, provider = _app(tmp_path)

    async def text_only_capabilities(model: str) -> ModelCapabilities:
        return ModelCapabilities(
            model=model,
            input_modalities=["text", "video"],
            output_modalities=["text"],
            streaming=True,
            extensions={"operations": {"video_understanding": {"supported": False}}},
        )

    provider.capabilities = text_only_capabilities  # type: ignore[method-assign]
    with TestClient(app) as client:
        request_id = "req-video-denied"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="video_understanding",
                input_blocks=[
                    {
                        "type": "video",
                        "mime_type": "video/mp4",
                        "source": {
                            "kind": "inline_base64",
                            "data": "AAAA",
                        },
                    }
                ],
                output_modality="text",
            ),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "MULTIMODAL_OPERATION_UNSUPPORTED"
        assert provider.calls == []


def test_provider_cannot_return_media_without_registered_output_asset(
    tmp_path: Path,
) -> None:
    app, provider = _app(tmp_path)

    async def invalid_execute(request, context: RequestContext) -> ProviderResult:
        del context
        return ProviderResult(
            status="completed",
            output=[
                {
                    "id": f"msg_{request.request_id}",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "image",
                            "mime_type": "image/png",
                            "source": {
                                "kind": "url",
                                "uri": "https://cdn.example.invalid/output.png",
                            },
                        }
                    ],
                }
            ],
        )

    provider.execute = invalid_execute  # type: ignore[method-assign]
    with TestClient(app) as client:
        request_id = "req-unregistered-output"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="image_generation",
                input_blocks=[{"type": "text", "text": "generate"}],
                output_modality="image",
            ),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["code"] == "PROVIDER_BAD_RESPONSE"


def test_stream_rejects_media_event_that_differs_from_terminal_item(
    tmp_path: Path,
) -> None:
    app, provider = _app(tmp_path)

    async def mismatched_stream(
        request, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        result = await provider.execute(request, context)
        event_item = result.output[0]
        yield ProviderEvent(
            kind=ProviderEventKind.MEDIA_COMPLETED,
            item_id=str(event_item["id"]),
            content_index=0,
            item=event_item,
        )
        terminal_item = dict(event_item)
        terminal_item["id"] = "msg_different"
        result.output = [terminal_item]
        yield ProviderEvent(kind=ProviderEventKind.COMPLETED, result=result)

    provider.stream = mismatched_stream  # type: ignore[method-assign]
    with TestClient(app) as client:
        request_id = "req-stream-mismatch"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="image_generation",
                input_blocks=[{"type": "text", "text": "generate"}],
                output_modality="image",
                stream=True,
            ),
        )
        assert response.status_code == 200
        events = [
            line[7:]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert events == [
            "response.created",
            "output_media.completed",
            "response.failed",
        ]


def test_stream_rejects_event_data_larger_than_one_mib(tmp_path: Path) -> None:
    app, provider = _app(tmp_path)

    async def oversized_stream(
        request, context: RequestContext
    ) -> AsyncIterator[ProviderEvent]:
        del request, context
        yield ProviderEvent(
            kind=ProviderEventKind.TEXT_DELTA,
            item_id="msg_large",
            content_index=0,
            delta="x" * (1024 * 1024),
        )

    provider.stream = oversized_stream  # type: ignore[method-assign]
    with TestClient(app) as client:
        request_id = "req-stream-large"
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=_request(
                request_id=request_id,
                capability="conversation",
                input_blocks=[{"type": "text", "text": "hello"}],
                output_modality="text",
                stream=True,
            ),
        )
        assert response.status_code == 200
        events = [
            line[7:]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert events == ["response.created", "response.failed"]


def test_model_response_json_larger_than_two_mib_is_rejected(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        request_id = "req-json-large"
        payload = _request(
            request_id=request_id,
            capability="conversation",
            input_blocks=[{"type": "text", "text": "x" * (2 * 1024 * 1024)}],
            output_modality="text",
        )
        response = client.post(
            "/model/responses",
            headers=_headers(request_id=request_id),
            json=payload,
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
