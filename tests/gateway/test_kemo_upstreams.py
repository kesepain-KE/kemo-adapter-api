"""Exercise federation through two real gateway applications, without paid calls."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from api.server import create_app
from core.config import PrincipalConfig, Settings
from core.models import AssetDescriptor, KemoRequest, ModelCapabilities, Usage
from core.provider_contract import (
    ProviderBatchResult, ProviderChoiceResult, ProviderEmbedding, ProviderEmbeddingResult,
    ProviderEvent, ProviderEventKind as Kind, ProviderRerankItem,
    ProviderRerankResult, ProviderResult, RequestContext,
)
from gateway.adapters.builtin.common import message
from gateway.adapters.builtin.kemo_v2 import KemoV2
from gateway.domain import Upstream
from gateway.transport import Transport
from tests.support.gateway import (
    SECRET, add_upstream, install_fixtures, instance, invoke, key, mutate, register, state,
)
from tests.support.media import WAV, MP4, TXT, assert_asset


MEDIA = {
    "image": ("image/png", b"\x89PNG\r\n\x1a\nfederation", "image_generation"),
    "audio": ("audio/wav", WAV, "speech_generation"),
    "video": ("video/mp4", MP4, "video_generation"),
    "file": ("text/plain", TXT, "conversation"),
}


class UpstreamProvider:
    provider_id = "federation-fixture"
    models = frozenset({"remote", "vectors", "scores"})

    def __init__(self, output=None):
        self.requests, self.input_assets = [], []
        self.output = output

    async def capabilities(self, model):
        task = {"vectors": "embedding", "scores": "rerank"}.get(model, "llm")
        common = {"protocol_version": "2.0", "model": model, "provider_id": self.provider_id,
                  "provider_model": model, "task": task}
        if task == "embedding":
            return ModelCapabilities(**common, output_modalities=["embedding"], embedding={
                "input_types": ["query", "document"], "default_dimensions": 3,
                "supported_dimensions": [3], "max_batch_size": 8, "normalization": "always",
            })
        if task == "rerank":
            return ModelCapabilities(**common, output_modalities=["score"],
                                     rerank={"max_documents": 32, "supports_score_threshold": True})
        caps = ModelCapabilities(
            **common, input_modalities=["text", "image", "audio", "video", "file"],
            output_modalities=["text", "image", "audio", "video", "file"], streaming=True,
            tools={"function_calling": True, "parallel_calls": True, "multimodal_results": True,
                   "tool_choice_modes": ["auto", "none", "named", "required", "allowed"]},
            reasoning={"supported": True, "efforts": ["adaptive-high", "high", "low"],
                       "returns": ["none", "auto", "summary"], "summary": True, "persisted_state": True},
            supports_multiple_choices=True, limits={"context_window": 64000, "max_choices": 4},
            extensions={"operations": {name: True for name in (
                "conversation", "vision", "image_generation", "image_edit", "audio_transcription",
                "speech_generation", "speech_to_speech", "video_understanding", "video_generation",
            )}},
        )
        if self.output:
            caps.output_modalities = [self.output]
            caps.extensions["operations"] = {MEDIA[self.output][2]: True}
        return caps

    async def execute(self, request, context):
        self.requests.append(request)
        for entry in request.input:
            for block in getattr(entry, "content", None) or []:
                if getattr(block, "asset_id", None):
                    resolved = context.assets.resolve(block.asset_id)
                    self.input_assets.append((resolved.descriptor.id, resolved.path.read_bytes()))
        if request.tools and not any(entry.type == "tool_result" for entry in request.input):
            return ProviderResult(status="requires_action", output=[{
                "type": "tool_call", "id": "call_native", "call_id": "callid_native",
                "name": request.tools[0].name, "arguments": {"query": "hello"},
            }])
        media = next((kind for kind in request.output.modalities if kind in MEDIA), None)
        if media:
            mime, content, _ = MEDIA[media]
            descriptor = await context.assets.store_output(filename="generated", mime_type=mime, content=content)
            blocks = [{"type": media, "asset_id": descriptor.id, "mime_type": mime,
                       "checksum_sha256": descriptor.checksum_sha256}]
            if media == "file":
                blocks.append({"type": "text", "text": "file generated"})
            return ProviderResult(status="completed", output=[{
                "type": "message", "id": "msg_media", "role": "assistant",
                "content": blocks,
            }])
        result = ProviderResult(status="completed", output=[message("hello", item_id="msg_answer")],
                                usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5))
        if request.reasoning is not None:
            result.output.insert(0, {
                "type": "reasoning", "id": "rs_native", "summary": "thinking",
                "provider_state": {"kind": "opaque", "data": "native-opaque", "provider": "gateway", "model": "remote"},
            })
        if request.generation.n > 1:
            return ProviderBatchResult([ProviderChoiceResult(i, result) for i in range(request.generation.n)],
                                       result.usage)
        return result

    async def stream(self, request, context):
        result = await self.execute(request, context)
        entry = result.output[0]
        if entry["type"] == "tool_call":
            yield ProviderEvent(Kind.TOOL_ARGUMENTS_DELTA, item_id=entry["id"], call_id=entry["call_id"],
                                name=entry["name"], delta='{"query":"hello"}')
            yield ProviderEvent(Kind.TOOL_COMPLETED, item_id=entry["id"], call_id=entry["call_id"],
                                name=entry["name"], item=entry)
        elif entry["content"][0]["type"] in MEDIA:
            yield ProviderEvent(Kind.MEDIA_COMPLETED, item_id=entry["id"], content_index=0, item=entry)
        else:
            yield ProviderEvent(Kind.TEXT_DELTA, item_id=entry["id"], content_index=0, delta="hello")
        yield ProviderEvent(Kind.USAGE, usage=result.usage)
        yield ProviderEvent(Kind.COMPLETED, result=result)

    async def embed(self, request, context):
        return ProviderEmbeddingResult(
            [ProviderEmbedding(i, [1., 0., 0.]) for i in range(len(request.inputs))], "vs_native",
            truncated_ids=[], truncation_reported=True, usage=Usage(input_tokens=2, total_tokens=2))

    async def rerank(self, request, context):
        return ProviderRerankResult(
            [ProviderRerankItem(i, .9 - i * .1) for i in range(len(request.documents))],
            usage=Usage(input_tokens=2, total_tokens=2))


@contextmanager
def federation(tmp_path, monkeypatch, provider=None):
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    b_app = create_app(Settings(api_keys={
        SECRET: PrincipalConfig("federated", "gateway-a", frozenset({"model:invoke", "asset:read", "asset:write"})),
    }), live_config_root=tmp_path / "b")
    provider, seen = provider or UpstreamProvider(), []
    install_fixtures(b_app.state.registry, provider)
    with TestClient(b_app) as b:
        async def forward(request):
            seen.append(request)
            response = await asyncio.to_thread(
                b.request, request.method, request.url.raw_path.decode().removeprefix("/local"),
                headers=dict(request.headers), content=await request.aread())
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        with instance(tmp_path / "a", monkeypatch, forward) as (a, app):
            add_upstream(a, adapter="kemo-v2")
            response = mutate(a, "/upstreams/local/register", {"models": [
                {"public_model": local, "upstream_model": remote}
                for local, remote in (("stable", "remote"), ("vectors", "vectors"), ("scores", "scores"))
            ]})
            assert response.status_code == 200, response.text
            yield a, app, provider, seen


def post(client, token, body):
    return client.post("/model/responses", headers={
        "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
        "Idempotency-Key": body["request_id"],
    }, json=body)


def request(identity="req_native", **fields):
    return {"protocol_version": "2.0", "request_id": identity, "model": "stable", "stream": False,
            "input": [{"type": "message", "id": "msg_user", "role": "user",
                       "content": [{"type": "text", "text": "hello"}]}], **fields}


def frames(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_native_declarations_text_stream_batch_and_request_namespace(tmp_path, monkeypatch):
    with federation(tmp_path, monkeypatch) as (client, app, provider, seen):
        assert provider.requests == []
        models = {item["id"]: item for item in state(client)["models"]}
        assert models["vectors"]["task"] == "embedding" and models["scores"]["task"] == "rerank"
        model = models["stable"]
        assert model["verification"] == "declared" and model["verified_at"] is None
        caps = model["capabilities"]
        assert caps["reasoning"]["efforts"] == ["low", "high", "adaptive-high"]
        assert caps["tools"]["parallel_calls"] and caps["tools"]["multimodal_results"]
        assert caps["limits"]["context_window"] == 64000
        assert caps["extensions"]["operations"]["video_generation"]
        token = key(client)["token"]
        result = invoke(client, token).json()
        assert result["status"] == "completed" and result["usage"]["total_tokens"] == 5
        upstream = provider.requests[-1]
        assert upstream.request_id != result["request_id"]
        assert upstream.model == "remote"
        assert seen[-1].headers["Idempotency-Key"] == upstream.request_id
        assert seen[-1].headers["X-Kemo-Federation-Hops"] == "1"
        stream = invoke(client, token, identity="req_stream", stream=True)
        events = frames(stream)
        assert events[-1]["type"] == "response.completed", stream.text
        assert [event["sequence"] for event in events] == list(range(len(events)))
        assert events[-1]["response_id"] != provider.requests[-1].request_id
        assert any(event["type"] == "output_text.done" for event in events)
        batch = post(client, token, request("req_batch", generation={"n": 2})).json()
        assert len(batch["responses"]) == 2, batch
        body = request("req_alias", reasoning={"enabled": True, "effort": "adaptive-high", "return": "summary"})
        result = post(client, token, body).json()
        assert result["status"] == "completed"
        assert provider.requests[-1].reasoning.return_mode == "summary"
        assert result["output"][0]["provider_state"]["model"] == "stable"
        body = request("req_state")
        body["input"].insert(0, result["output"][0])
        state_result = post(client, token, body).json()
        assert state_result["status"] == "completed", json.dumps(state_result)
        assert provider.requests[-1].input[0].provider_state.data == "native-opaque"
        adapter = app.state.registry.current.adapters["kemo-v2"]
        context = type("Context", (), {"tenant_id": "a", "subject_id": "a", "gateway_key_id": "key1"})()
        source = app.state.registry.current.upstreams["local"]
        first = adapter.request_id("req_same", context, "remote", source)
        assert first == adapter.request_id("req_same", context, "remote", source)
        context.gateway_key_id = "key2"
        assert first != adapter.request_id("req_same", context, "remote", source)
        body = request("req_json")
        body["input"][0]["content"].append({"type": "json", "data": {
            "type": "image", "asset_id": "asset_json_value",
        }})
        assert post(client, token, body).json()["status"] == "completed"
        assert provider.requests[-1].input[0].content[-1].data["asset_id"] == "asset_json_value"
        deployment = next(item for item in state(client)["deployments"] if item["public_model"] == "stable")
        before = len(provider.requests)
        report = mutate(client, f"/deployments/{deployment['id']}/probe")
        assert report.status_code == 200 and report.json()["source"] == "upstream-capabilities"
        assert len(provider.requests) == before


@pytest.mark.parametrize("stream", [False, True])
def test_federated_tools_and_followup_receipts(tmp_path, monkeypatch, stream):
    with federation(tmp_path, monkeypatch) as (client, _, provider, _):
        token = key(client)["token"]
        tool = {"type": "function", "name": "lookup", "description": "Look up a value.",
                "parameters": {"type": "object"}}
        body = request("req_tools", stream=stream, tools=[tool])
        response = post(client, token, body)
        assert response.status_code == 200, response.text
        result = frames(response)[-1]["response"] if stream else response.json()
        assert result["status"] == "requires_action", result
        call = result["output"][0]
        followup = request("req_followup", parent_request_id="req_tools", tools=[tool])
        followup["input"].extend([call, {
            "type": "tool_result", "id": "result_lookup", "call_id": call["call_id"], "name": "lookup",
            "content": [{"type": "text", "text": "found"}],
        }])
        result = post(client, token, followup).json()
        assert result["status"] == "completed", result
        assert provider.requests[-1].input[-1].call_id == provider.requests[-1].input[-2].call_id
        assert provider.requests[-1].parent_request_id == provider.requests[0].request_id


@pytest.mark.parametrize("kind", list(MEDIA))
@pytest.mark.parametrize("stream", [False, True])
def test_federated_assets_are_transferred_and_locally_owned(tmp_path, monkeypatch, kind, stream):
    mime, content, operation = MEDIA[kind]
    with federation(tmp_path, monkeypatch) as (client, _, provider, seen):
        token = key(client)["token"]
        upload = client.post("/assets", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "input-upload",
        }, files={"file": ("input", content, mime)})
        assert upload.status_code == 201, upload.text
        original = upload.json()["id"]
        config = {"image": {"format": "png"}, "audio": {"format": "wav"},
                  "video": {"format": "mp4"}, "file": {"mime_type": "text/plain", "filename": "result.txt"}}[kind]
        body = request("req_media", stream=stream, metadata={"capability": operation},
                       output={"modalities": ["text", "file"] if kind == "file" else [kind], kind: config})
        body["input"][0]["content"].append({"type": kind, "asset_id": original, "mime_type": mime})
        response = post(client, token, body)
        assert response.status_code == 200, response.text
        result = frames(response)[-1]["response"] if stream else response.json()
        assert result["status"] == "completed", result
        assert provider.input_assets[0][0] != original
        assert provider.input_assets[0][1] == content
        block = result["output"][0]["content"][0]
        assert_asset(client, token, block, content)
        if stream:
            media = next(event for event in frames(response) if event["type"] == "output_media.completed")
            assert media["item"]["content"][0]["asset_id"] == block["asset_id"]
        upload_call = next(call for call in seen if call.url.path.endswith("/assets") and call.method == "POST")
        assert "multipart/form-data" in upload_call.headers["content-type"]


def test_federated_retrieval_preserves_ids_spaces_and_local_filtering(tmp_path, monkeypatch):
    with federation(tmp_path, monkeypatch) as (client, _, _, _):
        token = key(client)["token"]
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}
        embedding = client.post("/model/embeddings", headers={**headers, "Idempotency-Key": "req_vectors"}, json={
            "protocol_version": "2.0", "request_id": "req_vectors", "model": "vectors",
            "input_type": "query", "inputs": [{"id": "query", "text": "hello"}],
        })
        assert embedding.status_code == 200, embedding.text
        assert embedding.json()["vector_space_id"] == "vs_native"
        assert embedding.json()["data"][0]["id"] == "query"
        rerank = client.post("/model/rerank", headers={**headers, "Idempotency-Key": "req_scores"}, json={
            "protocol_version": "2.0", "request_id": "req_scores", "model": "scores", "query": "hello",
            "documents": [{"id": "one", "text": "hello"}, {"id": "two", "text": "world"}],
            "top_n": 1, "score_threshold": .85, "return_documents": True,
        })
        assert rerank.status_code == 200, rerank.text
        assert [item["document_id"] for item in rerank.json()["results"]] == ["one"]


def test_self_reference_and_inbound_hop_validation(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            result = mutate(client, "/upstreams", {
                "id": "self", "name": "self", "adapter_id": "kemo-v2",
                "base_url": f"http://{host}:7531", "allow_private_network": True,
            })
            assert result.status_code == 422 and result.json()["detail"]["code"] == "UPSTREAM_SELF_REFERENCE"
        for hops in ("3", "-1", "1,2", "01"):
            result = client.get("/model/models", headers={"X-Kemo-Federation-Hops": hops})
            assert result.status_code == 400 and result.json()["error"]["code"] == "PROTOCOL_ERROR"


def test_http_error_keeps_kemo_code_details_and_stops_retry(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(400, json={
            "protocol_version": "2.0", "object": "kemo.error",
            "error": {"type": "federation", "code": "PROTOCOL_ERROR", "message": "refused",
                      "retryable": False, "details": {"kind": "upstream-policy"}},
        })
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="kemo-v2")
        register(client)
        response = invoke(client, key(client)["token"]).json()
        assert response["status"] == "failed"
        assert response["error"]["code"] == "PROTOCOL_ERROR"
        assert response["error"]["details"]["kind"] == "upstream-policy"
        assert response["error"]["retryable"] is False and len(seen) == 1


def test_two_gateway_cycle_stops_at_hop_limit(tmp_path, monkeypatch):
    clients, seen = {}, []
    async def bridge(request, destination):
        seen.append(request.headers["X-Kemo-Federation-Hops"])
        response = await asyncio.to_thread(
            clients[destination].request, request.method,
            request.url.raw_path.decode().removeprefix("/local"),
            content=await request.aread(), headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    settings = Settings(api_keys={
        SECRET: PrincipalConfig("federation", "gateway", frozenset({"model:invoke"})),
    })
    with instance(tmp_path / "a", monkeypatch, lambda req: bridge(req, "b"), settings) as (a, _):
        with instance(tmp_path / "b", monkeypatch, lambda req: bridge(req, "a"), settings) as (b, _):
            clients.update(a=a, b=b)
            for client in (a, b):
                add_upstream(client, adapter="kemo-v2")
                register(client, model="remote")
            result = invoke(a, SECRET, model="remote").json()
            assert result["status"] == "failed", result
            assert result["error"]["code"] == "PROTOCOL_ERROR"
            assert result["error"]["retryable"] is False
            assert seen == ["1", "2"], json.dumps(result)


@pytest.mark.parametrize("kind", ["image", "audio", "video"])
def test_model_test_uses_declared_media_operation(tmp_path, monkeypatch, kind):
    with federation(tmp_path, monkeypatch, UpstreamProvider(output=kind)) as (client, _, provider, _):
        assert provider.requests == []
        deployment = next(item for item in state(client)["deployments"] if item["public_model"] == "stable")
        response = mutate(client, f"/deployments/{deployment['id']}/test")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "supported", response.text
        assert provider.requests[-1].output.modalities == [kind]
        assert provider.requests[-1].metadata["capability"] == MEDIA[kind][2]
        assert len(provider.requests) == 1


def test_closing_native_stream_cancels_upstream_and_closes_connection():
    seen, closed = [], []
    class PartialStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            body = json.loads(seen[0].content)
            common = {"request_id": body["request_id"], "response_id": "resp_upstream"}
            events = [
                {"type": "response.created", "event_id": "evt_created", "sequence": 0, **common},
                {"type": "output_item.added", "event_id": "evt_item", "sequence": 1, "previous_sequence": 0,
                 "item_id": "msg_answer", "item": {"id": "msg_answer", "type": "message", "role": "assistant"},
                 **common},
            ]
            yield "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.append(True)
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(200, stream=PartialStream(), headers={"Content-Type": "text/event-stream"})
    async def run():
        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            adapter = KemoV2(transport)
            context = RequestContext("tenant", "subject", "req_local", "resp_local", "trace")
            upstream = Upstream(id="native", name="Native", adapter_id="kemo-v2",
                                base_url="https://upstream.example")
            events = adapter.stream(KemoRequest.model_validate(request(stream=True)), context, "remote", upstream, {})
            assert (await anext(events)).kind == Kind.ITEM_ADDED
            await events.aclose()
        finally:
            await transport.close()
    asyncio.run(run())
    assert closed == [True]
    assert len(seen) == 2 and seen[-1].url.path == "/model/responses/resp_upstream/cancel"


@pytest.mark.parametrize("corrupt", [False, True])
def test_asset_download_failure_never_repeats_generation(tmp_path, monkeypatch, corrupt):
    generations = []
    descriptor = AssetDescriptor(
        protocol_version="2.0", id="asset_remote", purpose="output", status="ready",
        filename="speech.wav", mime_type="audio/wav", size=len(WAV),
        checksum_sha256=hashlib.sha256(WAV).hexdigest(),
        created_at="2026-11-09T00:00:00Z", expires_at="2026-11-10T00:00:00Z")
    def handler(request):
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=WAV[:-1]) if corrupt else httpx.Response(503)
        if request.url.path.endswith("/asset_remote"):
            return httpx.Response(200, json=descriptor.model_dump(mode="json"))
        body = json.loads(request.content)
        generations.append(body)
        return httpx.Response(200, json={
            "protocol_version": "2.0", "id": "resp_remote", "request_id": body["request_id"],
            "model": body["model"], "status": "completed", "output": [{
                "type": "message", "id": "msg_speech", "role": "assistant", "content": [{
                    "type": "audio", "mime_type": "audio/wav", "asset_id": descriptor.id,
                    "checksum_sha256": descriptor.checksum_sha256,
                }],
            }],
        })
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="kemo-v2")
        register(client, capabilities={"output_modalities": ["audio"],
                                       "extensions": {"operations": {"speech_generation": True}}})
        response = post(client, key(client)["token"], request(
            "req_speech", metadata={"capability": "speech_generation"},
            output={"modalities": ["audio"], "audio": {"format": "wav"}})).json()
        assert response["status"] == "failed", response
        assert response["error"]["retryable"] is False
        assert len(generations) == 1 and len(state(client)["attempts"]) == 1
