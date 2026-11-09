from __future__ import annotations

import base64
import io
import json
import wave

import httpx
import pytest

from tests.support.gateway import add_upstream, instance, key, mutate, register, state
from tests.support.media import WAV, MP4, TXT, assert_asset, call, multipart


def test_speech_registration_binary_output_and_real_audio_events(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        assert request.url.path.endswith("/audio/speech")
        assert body["response_format"] == "wav" and body["voice"] == "alloy"
        seen.append(request)
        return httpx.Response(200, content=WAV, headers={"Content-Type": "audio/wav"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-speech")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "public_model": "media", "upstream_model": "speech-engine",
        }]})
        assert response.status_code == 200, response.text
        model = state(client)["models"][0]
        assert model["verification"] == "verified"
        assert model["capabilities"]["output_modalities"] == ["audio"]
        assert model["capabilities"]["streaming"] is True
        assert response.json()["detection"][0]["checks"]["output.audio"]["status"] == "supported"
        token = key(client)["token"]
        response = call(client, token, "req_speech", "speech_generation", ["audio"],
                        config={"audio": {"format": "wav"}})
        assert response.status_code == 200, response.text
        complete = response.json()
        assert complete["status"] == "completed", complete
        assert complete["usage"]["media"]["output_audio_seconds"] == 1
        assert complete["usage"]["output_tokens"] is None
        assert_asset(client, token, complete["output"][0]["content"][0], WAV)
        response = call(client, token, "req_speech_stream", "speech_generation", ["audio"], stream=True,
                        config={"audio": {"format": "wav"}})
        assert "event: output_audio.delta" in response.text
        assert "event: output_media.completed" in response.text
        assert "event: response.completed" in response.text, response.text
        assert len(seen) == 4


def test_transcription_probe_and_audio_chat_publish_owned_outputs(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        seen.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            parts = multipart(request)
            assert parts["file"].startswith(b"RIFF") and parts["response_format"] == b"json"
            return httpx.Response(200, json={"text": "copper meadow", "usage": {"type": "duration", "seconds": 3}})
        body = json.loads(request.content)
        assert body["messages"][0]["content"][1]["type"] == "input_audio"
        return httpx.Response(200, json={"id": "native-audio", "choices": [{"message": {
            "audio": {"data": base64.b64encode(WAV).decode(), "transcript": "hello"}}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5}})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-transcription")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "public_model": "transcriber", "upstream_model": "audio-engine",
        }]})
        assert response.status_code == 200, response.text
        assert response.json()["detection"][0]["checks"]["audio_transcription"]["status"] == "supported"
        assert state(client)["models"][0]["verification"] == "verified"
        add_upstream(client, identity="chat", adapter="openai-audio-chat")
        response = mutate(client, "/upstreams/chat/register", {"models": [{
            "public_model": "media", "upstream_model": "audio-chat-engine",
        }]})
        assert response.status_code == 200, response.text
        model = next(model for model in state(client)["models"] if model["id"] == "media")
        assert model["capabilities"]["extensions"]["operations"]["speech_to_speech"]
        token = key(client)["token"]
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
                   "Idempotency-Key": "upload_audio"}
        upload = client.post("/assets", headers=headers, files={"file": ("input.wav", WAV, "audio/wav")})
        assert upload.status_code == 201, upload.text
        response = call(client, token, "req_audio_chat", "speech_to_speech", ["audio"], blocks=[
            {"type": "audio", "asset_id": upload.json()["id"], "mime_type": "audio/wav"}],
            config={"audio": {"format": "wav"}})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed", response.text
        assert_asset(client, token, response.json()["output"][0]["content"][0], WAV)
        assert response.json()["usage"]["total_tokens"] == 8


@pytest.mark.parametrize("broken_download", [False, True])
def test_video_jobs_submit_once_poll_and_download_without_rebilling(tmp_path, monkeypatch, broken_download):
    submitted, polled = [], []
    def handler(request):
        if request.method == "POST":
            submitted.append(request)
            assert multipart(request)["seconds"] == b"4"
            return httpx.Response(200, json={"id": "video_123", "status": "queued"})
        if request.url.path.endswith("/content"):
            return httpx.Response(502) if broken_download else httpx.Response(200, content=MP4)
        polled.append(request)
        return httpx.Response(200, json={"id": "video_123", "status": "completed", "seconds": "4"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-video")
        assert mutate(client, "/upstreams/local", {"profile": {"poll_interval": 0.1}, "keys": [
            {"id": "primary"}, {"id": "second", "value": "second-test-key"},
        ]}, method="PATCH").status_code == 200
        register(client, model="media", capabilities={"streaming": False, "output_modalities": ["video"],
            "extensions": {"operations": {"video_generation": True}}})
        token = key(client)["token"]
        response = call(client, token, "req_video", "video_generation", ["video"],
                        config={"video": {"format": "mp4", "duration_seconds": 4}})
        assert response.status_code == 200, response.text
        assert len(submitted) == 1 and len(polled) == 1
        complete = response.json()
        if broken_download:
            assert complete["status"] == "failed", complete
            assert complete["error"]["retryable"] is False
            assert complete["error"]["details"]["provider_job_id"] == "video_123"
        else:
            assert complete["status"] == "completed", complete
            assert complete["usage"]["media"]["output_video_seconds"] == 4
            assert_asset(client, token, complete["output"][0]["content"][0], MP4)


def test_responses_generates_downloadable_files_and_forwards_pdf_inputs(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        if request.method == "GET":
            assert request.url.path.endswith("/containers/cntr_1/files/cfile_1/content")
            return httpx.Response(200, content=TXT, headers={"Content-Type": "text/plain"})
        body = json.loads(request.content)
        if "tools" not in body:
            assert body["input"][0]["content"][1]["type"] == "input_file"
            return httpx.Response(200, json={"status": "completed", "output": [{
                "type": "message", "content": [{"type": "output_text", "text": "LARCH-5821"}]}]})
        assert body["tools"] == [{"type": "code_interpreter", "container": {"type": "auto"}}]
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "Download the file.", "annotations": [{
                "type": "container_file_citation", "container_id": "cntr_1",
                "file_id": "cfile_1", "filename": "probe.txt",
            }]}]}], "usage": {"input_tokens": 4, "output_tokens": 6}})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-files")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "public_model": "media", "upstream_model": "file-engine",
        }]})
        assert response.status_code == 200, response.text
        checks = response.json()["detection"][0]["checks"]
        assert checks["output.file"]["status"] == checks["file"]["status"] == "supported"
        token = key(client)["token"]
        response = call(client, token, "req_file", "conversation", ["text", "file"],
                        config={"file": {"filename": "probe.txt", "mime_type": "text/plain"}})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed", response.text
        block = response.json()["output"][0]["content"][-1]
        assert block["type"] == "file" and block["filename"] == "probe.txt"
        assert_asset(client, token, block, TXT)
        response = call(client, token, "req_file_stream", "conversation", ["text", "file"], stream=True,
                        config={"file": {"filename": "probe.txt"}})
        assert "event: output_media.completed" in response.text
        assert "event: response.completed" in response.text, response.text


@pytest.mark.parametrize("status,expected", [(400, "unsupported"), (401, "unknown"), (502, "unknown")])
def test_media_generation_probe_keeps_transient_errors_unknown(tmp_path, monkeypatch, status, expected):
    with instance(tmp_path, monkeypatch, lambda request: httpx.Response(
        200, json={"data": []}) if request.method == "GET" else httpx.Response(status),
    ) as (client, _):
        add_upstream(client, adapter="openai-speech")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "public_model": "media", "upstream_model": "unlisted-speech",
        }]})
        assert response.status_code == 200, response.text
        assert response.json()["detection"][0]["checks"]["output.audio"]["status"] == expected
        assert state(client)["models"][0]["verification"] == "unknown"


def test_gemini_speech_wraps_native_pcm_in_wav_and_counts_actual_duration(tmp_path, monkeypatch):
    pcm = b"\0\0" * 24000
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"inputTokenLimit": 1000, "outputTokenLimit": 1000})
        body = json.loads(request.content)
        assert body["generationConfig"]["responseModalities"] == ["AUDIO"]
        assert body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{
            "inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()},
        }]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7}})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="gemini-speech", auth_scheme="gemini-key")
        response = mutate(client, "/upstreams/local/register", {"models": [{
            "public_model": "media", "upstream_model": "speech-native",
        }]})
        assert response.status_code == 200, response.text
        assert state(client)["models"][0]["verification"] == "verified"
        token = key(client)["token"]
        response = call(client, token, "req_native_speech", "speech_generation", ["audio"],
                        config={"audio": {"format": "wav"}})
        assert response.json()["status"] == "completed", response.text
        assert response.json()["usage"]["media"]["output_audio_seconds"] == 1
        block = response.json()["output"][0]["content"][0]
        content = client.get(f"/assets/{block['asset_id']}/content", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}).content
        with wave.open(io.BytesIO(content), "rb") as audio:
            assert audio.getframerate() == 24000 and audio.getnchannels() == 1
            assert audio.readframes(24000) == pcm


@pytest.mark.parametrize("uri", [
    "http://127.0.0.1:8881/local/files/video_1",
    "http://169.254.169.254/files/video_1",
])
def test_gemini_video_downloads_only_from_the_configured_upstream(tmp_path, monkeypatch, uri):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert request.url.path.endswith(":predictLongRunning")
            assert json.loads(request.content)["parameters"]["numberOfVideos"] == 1
            return httpx.Response(200, json={"name": "models/video-native/operations/job_1"})
        if request.url.path.endswith(":download"):
            assert request.url.params["alt"] == "media"
            return httpx.Response(200, content=MP4)
        return httpx.Response(200, json={"name": "models/video-native/operations/job_1", "done": True,
            "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}}})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="gemini-video", auth_scheme="gemini-key")
        assert mutate(client, "/upstreams/local", {"profile": {"poll_interval": 0.1}}, method="PATCH").status_code == 200
        register(client, model="media", capabilities={"streaming": False, "output_modalities": ["video"],
            "extensions": {"operations": {"video_generation": True}}})
        token = key(client)["token"]
        response = call(client, token, "req_native_video", "video_generation", ["video"],
                        config={"video": {"format": "mp4", "duration_seconds": 4}})
        assert response.status_code == 200, response.text
        if "169.254" in uri:
            assert response.json()["status"] == "failed"
            assert response.json()["error"]["retryable"] is False
            assert len(calls) == 2
        else:
            assert response.json()["status"] == "completed", response.text
            assert_asset(client, token, response.json()["output"][0]["content"][0], MP4)
            assert len(calls) == 3


def test_timeout_after_video_submission_never_submits_another_paid_job(tmp_path, monkeypatch):
    submitted = []
    def handler(request):
        submitted.append(request)
        return httpx.Response(200, json={"id": "video_waiting", "status": "queued"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-video")
        assert mutate(client, "/upstreams/local", {"timeout": 0.05, "keys": [
            {"id": "primary"}, {"id": "second", "value": "other-test-key"},
        ]}, method="PATCH").status_code == 200
        register(client, model="media", capabilities={"streaming": False, "output_modalities": ["video"],
            "extensions": {"operations": {"video_generation": True}}})
        response = call(client, key(client)["token"], "req_video_timeout", "video_generation", ["video"],
                        config={"video": {"format": "mp4", "duration_seconds": 4}})
        assert response.json()["status"] == "failed", response.text
        assert response.json()["error"]["retryable"] is False
        assert len(submitted) == 1


@pytest.mark.parametrize("adapter,operation,modality,config,raw", [
    ("openai-speech", "speech_generation", "audio", {"audio": {"format": "wav"}}, b"not audio"),
    ("openai-files", "conversation", "file", {"file": {}}, b"not JSON"),
])
def test_invalid_media_response_is_not_retried_or_published(tmp_path, monkeypatch, adapter, operation, modality, config, raw):
    called = []
    def handler(request):
        called.append(request)
        return httpx.Response(200, content=raw)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter=adapter)
        assert mutate(client, "/upstreams/local", {"keys": [
            {"id": "primary"}, {"id": "second", "value": "other-test-key"},
        ]}, method="PATCH").status_code == 200
        modalities = ["text", "file"] if modality == "file" else ["audio"]
        caps = {"streaming": False, "output_modalities": modalities}
        if operation != "conversation":
            caps["extensions"] = {"operations": {operation: True}}
        register(client, model="media", capabilities=caps)
        response = call(client, key(client)["token"], "req_bad_media", operation, modalities, config=config)
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["retryable"] is False
        assert len(called) == 1
