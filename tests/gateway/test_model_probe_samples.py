from __future__ import annotations

import base64
import json

import httpx
import pytest

from core.assets import AssetStoreFailure
from tests.support.gateway import add_upstream, chat_response, instance, mutate, state
from tests.support.paths import PROJECT_ROOT


SAMPLES = {
    "audio/wav": ("audio", "audio.wav", "copper meadow", "audio-code-v1"),
    "video/mp4": ("video", "video.mp4", "red, green, blue", "video-colors-v1"),
    "application/pdf": ("file", "document.pdf", "LARCH-5821", "document-code-v1"),
}


def media_payload(request):
    body = json.loads(request.content)
    if "contents" in body:
        parts = [part for content in body["contents"] for part in content["parts"]]
        prompt = "\n".join(part["text"] for part in parts if "text" in part)
        for part in parts:
            value = part.get("inlineData")
            if value and value["mimeType"] in SAMPLES:
                return value["mimeType"], base64.b64decode(value["data"]), prompt
    else:
        blocks = [block for message in body.get("messages", []) for block in message.get("content", [])]
        prompt = "\n".join(block["text"] for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "input_audio":
                assert block["input_audio"]["format"] == "wav"
                return "audio/wav", base64.b64decode(block["input_audio"]["data"]), prompt
            if block.get("type") == "file":
                assert block["file"]["filename"] == "document.pdf"
                prefix, encoded = block["file"]["file_data"].split(",", 1)
                assert prefix == "data:application/pdf;base64"
                return "application/pdf", base64.b64decode(encoded), prompt
    return None


def reply(adapter, text):
    if adapter == "gemini":
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
        })
    return httpx.Response(200, json=chat_response(text))


@pytest.mark.parametrize(("adapter", "modalities"), [
    ("openai-chat", ["audio", "file"]),
    ("gemini", ["audio", "video", "file"]),
])
def test_builtin_samples_reach_real_adapters_and_confirm_registration_and_reprobe(tmp_path, monkeypatch, adapter, modalities):
    received = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        media = media_payload(request)
        if media:
            mime, content, prompt = media
            modality, filename, answer, _ = SAMPLES[mime]
            assert content == (PROJECT_ROOT / "gateway/probe_samples" / filename).read_bytes()
            assert not any(word in prompt.lower() for word in ("copper", "meadow", "larch", "5821", "red", "green", "blue"))
            assert str(PROJECT_ROOT) not in request.content.decode()
            received.append(modality)
            return reply(adapter, answer)
        body = json.loads(request.content)
        if body.get("stream") or body.get("tools") or body.get("response_format") or "generationConfig" in body and (
            body["generationConfig"].get("responseMimeType")
        ) or "streamGenerateContent" in request.url.path:
            return httpx.Response(400, json={"error": "not supported"})
        return reply(adapter, "OK")

    with instance(tmp_path, monkeypatch, handler) as (client, app):
        add_upstream(client, adapter=adapter, auth_scheme="none")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "upstream_model": "remote", "public_model": "sample-model",
        }]})
        assert response.status_code == 200, response.text
        registered = state(client)
        model = registered["models"][0]
        assert model["capabilities"]["input_modalities"] == ["text", *modalities]
        operations = model["capabilities"]["extensions"]["operations"]
        assert operations["audio_transcription"] is True
        assert operations.get("video_understanding", False) == ("video" in modalities)
        assert received == modalities
        evidence = json.loads(model["evidence"])["checks"]
        for mime, (modality, _, _, sample_id) in SAMPLES.items():
            if modality in modalities:
                assert evidence[modality] == {
                    "status": "supported", "reason": "response-confirmed", "sample_id": sample_id,
                }
            else:
                assert evidence[modality]["reason"] == "adapter-not-implemented"
        records = sorted(app.state.assets.root.glob("*/*/metadata.json"))
        assert len(records) == len(modalities)
        for record in records:
            descriptor = json.loads(record.read_text(encoding="utf-8"))["descriptor"]
            assert app.state.assets.bind("gateway", "capability-probe").resolve(descriptor["id"]).path.is_file()
            with pytest.raises(AssetStoreFailure) as error:
                app.state.assets.bind("gateway", "another-subject").resolve(descriptor["id"])
            assert error.value.error.code == "ASSET_NOT_FOUND"
        deployment = registered["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/probe")
        assert response.status_code == 200, response.text
        assert response.json()["capabilities"] == model["capabilities"]
        assert received == modalities * 2
        assert sorted(app.state.assets.root.glob("*/*/metadata.json")) == records
        after = state(client)
        for key in ("revision", "models", "deployments", "upstreams", "settings"):
            assert after[key] == registered[key]
        reasoning_checks = 6 if adapter == "openai-chat" else 0
        assert len(after["attempts"]) == 2 * (5 + len(modalities) + reasoning_checks)
        assert all(attempt["status"] != "running" for attempt in after["attempts"])


def test_generic_or_incorrect_media_answers_do_not_confirm_capabilities(tmp_path, monkeypatch):
    answers = {"audio/wav": "OK", "video/mp4": "blue, green, red", "application/pdf": "LARCH-5822"}
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        media = media_payload(request)
        return reply("gemini", answers[media[0]] if media else "OK")
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="gemini", auth_scheme="none")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "upstream_model": "remote", "public_model": "sample-model",
        }]})
        assert response.status_code == 200, response.text
        checks = response.json()["detection"][0]["checks"]
        for modality, _, _, sample_id in SAMPLES.values():
            assert checks[modality] == {
                "status": "unknown", "reason": "response-not-confirmed", "sample_id": sample_id,
            }
        assert state(client)["models"][0]["capabilities"]["input_modalities"] == ["text"]


def test_one_media_error_does_not_hide_other_sample_results_or_metering(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        media = media_payload(request)
        if media and media[0] == "audio/wav":
            return httpx.Response(415, json={"error": "private vendor response"})
        if media and media[0] == "video/mp4":
            raise httpx.ReadTimeout("private timeout", request=request)
        return reply("gemini", "LARCH-5821" if media else "OK")
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="gemini", auth_scheme="none")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "upstream_model": "remote", "public_model": "sample-model",
        }]})
        assert response.status_code == 200, response.text
        checks = response.json()["detection"][0]["checks"]
        assert checks["audio"]["status"] == "unsupported" and checks["audio"]["provider_status"] == 415
        assert checks["video"]["status"] == "unknown"
        assert checks["file"]["status"] == "supported"
        assert "private" not in response.text
        current = state(client)
        assert current["models"][0]["capabilities"]["input_modalities"] == ["text", "file"]
        assert len(current["attempts"]) == 8
        # The fixture returns plain JSON for streaming, so its missing SSE terminal also fails.
        assert checks["streaming"]["reason"] == "PROVIDER_BAD_RESPONSE"
        assert sum(attempt["status"] == "failed" for attempt in current["attempts"]) == 3
