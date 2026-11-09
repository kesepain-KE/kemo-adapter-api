from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from core.config import PrincipalConfig, Settings
from tests.support.gateway import add_upstream, chat_response, instance, mutate, register, state


@pytest.mark.parametrize("adapter", ["openai-chat", "gemini"])
def test_native_media_inputs_reach_upstream_and_unsupported_outputs_are_rejected(tmp_path, monkeypatch, adapter):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if adapter == "gemini":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "media understood"}]},
                                                           "finishReason": "STOP"}]})
        return httpx.Response(200, json=chat_response("media understood"))
    settings = Settings(api_keys={"media-test": PrincipalConfig(
        "test", "agent", frozenset({"model:invoke", "asset:write", "asset:read"}),
    )})
    media = [
        ("audio", "audio/wav", b"RIFF\x10\x00\x00\x00WAVEfmt "),
        ("file", "application/pdf", b"%PDF-1.7\n% test\n"),
    ]
    if adapter == "gemini":
        media.append(("video", "video/mp4", b"\x00\x00\x00\x18ftypisom"))
    capabilities = {
        "streaming": False, "input_modalities": ["text", *(kind for kind, _, _ in media)],
        "output_modalities": ["text"],
        "extensions": {"operations": {"audio_transcription": True, **({"video_understanding": True} if adapter == "gemini" else {})}},
    }
    with instance(tmp_path, monkeypatch, handler, settings) as (client, _):
        add_upstream(client, adapter=adapter, auth_scheme="api-key" if adapter == "gemini" else "bearer")
        register(client, capabilities=capabilities)
        blocks = []
        for kind, mime, content in media:
            filename = {"audio": "sample.wav", "video": "sample.mp4", "file": "sample.pdf"}[kind]
            uploaded = client.post("/assets", headers={
                "Authorization": "Bearer media-test", "X-Kemo-Protocol-Version": "2.0",
                "Idempotency-Key": "asset_" + kind, "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
            }, data={"metadata": json.dumps({"purpose": "input", "session_id": "s1"})},
                files={"file": (filename, content, mime)})
            assert uploaded.status_code == 201, uploaded.text
            blocks.append({"type": kind, "asset_id": uploaded.json()["id"], "mime_type": mime,
                           **({"filename": filename} if kind == "file" else {})})
        body = {
            "protocol_version": "2.0", "request_id": "req_native_media", "model": "stable", "stream": False,
            "metadata": {"session_id": "s1"},
            "input": [{"type": "message", "id": "msg_media", "role": "user",
                       "content": [{"type": "text", "text": "Describe these inputs."}, *blocks]}],
        }
        response = client.post("/model/responses", json=body, headers={
            "Authorization": "Bearer media-test", "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "req_native_media",
        })
        assert response.status_code == 200, response.text
        assert response.json()["output"][0]["content"][0]["text"] == "media understood"
        assert len(calls) == 1
        if adapter == "gemini":
            parts = calls[0]["contents"][0]["parts"][1:]
            assert parts == [{"inlineData": {"mimeType": mime, "data": base64.b64encode(content).decode()}}
                             for _, mime, content in media]
        else:
            parts = calls[0]["messages"][0]["content"][1:]
            assert parts[0] == {"type": "input_audio", "input_audio": {
                "data": base64.b64encode(media[0][2]).decode(), "format": "wav",
            }}
            assert parts[1] == {"type": "file", "file": {
                "filename": "sample.pdf",
                "file_data": "data:application/pdf;base64," + base64.b64encode(media[1][2]).decode(),
            }}
        model = state(client)["models"][0]
        response = mutate(client, "/models/stable", {"capabilities": {
            **model["capabilities"], "output_modalities": ["text", "audio"],
        }}, method="PATCH")
        assert response.status_code == 422
        assert len(calls) == 1 and state(client)["models"][0] == model


def test_extended_chat_inputs_do_not_change_perplexity_contract(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        add_upstream(client, adapter="perplexity-agent")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "upstream_model": "remote", "public_model": "stable",
            "capabilities": {"input_modalities": ["text", "audio"]},
        }]})
        assert response.status_code == 422 and state(client)["models"] == []
