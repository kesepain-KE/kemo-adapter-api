from __future__ import annotations

import base64
import json
import struct
import zlib

import httpx
import pytest

from tests.support.gateway import add_upstream, chat_response, instance, mutate, register, state


def image_color(uri):
    png = base64.b64decode(uri.split(",", 1)[1])
    offset = 8
    while offset < len(png):
        size = struct.unpack(">I", png[offset:offset + 4])[0]
        if png[offset + 4:offset + 8] == b"IDAT":
            rgb = tuple(zlib.decompress(png[offset + 8:offset + 8 + size])[1:4])
            return {(255, 0, 0): "red", (0, 255, 0): "green", (0, 0, 255): "blue"}[rgb]
        offset += size + 12
    raise AssertionError("No PNG image data")


def capable_upstream(request):
    if request.method == "GET":
        return httpx.Response(200, json={"data": [{
            "id": "remote", "context_length": 65536, "top_provider": {"max_completion_tokens": 4096},
        }]})
    body = json.loads(request.content)
    if body.get("stream"):
        frames = [
            {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        ]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames))
    if body.get("tools"):
        raw = chat_response()
        raw["choices"][0]["message"] = {"role": "assistant", "tool_calls": [{
            "id": "probe_call", "type": "function",
            "function": {"name": "kemo_capability_probe", "arguments": '{"value":"ok"}'},
        }]}
        raw["choices"][0]["finish_reason"] = "tool_calls"
        return httpx.Response(200, json=raw)
    if body.get("response_format"):
        return httpx.Response(200, json=chat_response('{"ok":true}'))
    for message in body["messages"]:
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "image_url":
                return httpx.Response(200, json=chat_response(image_color(block["image_url"]["url"])))
    return httpx.Response(200, json=chat_response("OK"))


def automatic_registration(client, model="detected", expected=None):
    return mutate(client, "/upstreams/local/register", {"models": [{
        "upstream_model": "remote", "public_model": model,
    }]}, expected=expected)


def test_selected_model_is_probed_registered_and_editable(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return capable_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, app):
        add_upstream(client)
        assert client.post("/admin/api/upstreams/local/discover").json()["models"] == ["remote"]
        assert len(calls) == 1
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        model = state(client)["models"][0]
        caps = model["capabilities"]
        assert model["verification"] == "verified" and model["verified_at"]
        assert caps["streaming"] is True
        assert caps["tools"]["function_calling"] is True
        assert caps["structured_output"]["schema_subset"] == ["json_schema"]
        assert caps["input_modalities"] == ["text", "image"]
        assert caps["limits"] == {"context_window": 65536, "max_output_tokens": 4096}
        assert len(state(client)["attempts"]) == 14
        assert all(attempt["source"] == "exact" for attempt in state(client)["attempts"])
        report = response.json()["detection"][0]
        assert report["checks"]["vision"]["status"] == "supported"
        assert app.state.registry._references.get(id(app.state.registry.current), 0) == 0
        updated = mutate(client, "/models/detected", {"capabilities": {
            **caps, "limits": {**caps["limits"], "context_window": 32768},
        }}, method="PATCH")
        assert updated.status_code == 200, updated.text
        model = state(client)["models"][0]
        assert model["capabilities"]["limits"]["context_window"] == 32768
        assert model["verification"] == "declared"
        assert model["verified_at"] is None


def test_ignored_options_and_missing_metadata_do_not_invent_capabilities(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "remote"}]})
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(400, json={"error": "Streaming is not supported"})
        return httpx.Response(200, json=chat_response("OK"))
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        caps = state(client)["models"][0]["declaration"]
        assert caps["streaming"] is False
        assert caps["tools"]["function_calling"] is False
        assert caps["structured_output"]["supported"] is False
        assert caps["reasoning"]["supported"] is False
        assert caps["input_modalities"] == ["text"]
        assert caps["limits"]["context_window"] is None
        assert response.json()["detection"][0]["checks"]["tools"]["status"] == "unknown"
        assert all(response.json()["detection"][0]["checks"]["reasoning." + effort]["status"] == "unknown"
                   for effort in ("minimal", "low", "medium", "high", "xhigh", "max"))


@pytest.mark.parametrize("values,expected_status", [
    (["first", "second"], "supported"),
    (["first"], "unknown"),
    ([], "unknown"),
    (None, "unsupported"),
])
def test_parallel_tools_require_two_confirmed_calls_and_reach_public_capabilities(
    tmp_path, monkeypatch, values, expected_status,
):
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("tools") and body.get("parallel_tool_calls"):
                assert body["tool_choice"] == "auto"
                if values is None:
                    return httpx.Response(400, json={"error": "Parallel tools unsupported"})
                raw = chat_response()
                raw["choices"][0]["message"] = {"role": "assistant", "tool_calls": [
                    {"id": "call_" + value, "type": "function", "function": {
                        "name": "kemo_capability_probe", "arguments": json.dumps({"value": value}),
                    }} for value in values
                ]}
                return httpx.Response(200, json=raw)
        return capable_upstream(request)

    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        checks = response.json()["detection"][0]["checks"]
        assert checks["tools"]["status"] == "supported"
        assert checks["tools.parallel_calls"]["status"] == expected_status
        expected_parallel = expected_status == "supported"
        model = state(client)["models"][0]
        assert model["capabilities"]["tools"]["function_calling"] is True
        assert model["capabilities"]["tools"]["parallel_calls"] is expected_parallel
        token = mutate(client, "/keys", {"name": "tool capability reader"}, key=True).json()["token"]
        published = client.get("/model/models/detected/capabilities", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
        })
        assert published.status_code == 200, published.text
        assert published.json()["tools"]["function_calling"] is True
        assert published.json()["tools"]["parallel_calls"] is expected_parallel
        generated = client.post("/model/responses", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "req_tools",
        }, json={
            "protocol_version": "2.0", "request_id": "req_tools", "model": "detected", "stream": False,
            "input": [{"type": "message", "id": "msg_user", "role": "user",
                       "content": [{"type": "text", "text": "Call the probe"}]}],
            "tools": [{"type": "function", "name": "kemo_capability_probe",
                       "description": "Return the probe value.",
                       "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}],
            "parallel_tool_calls": expected_parallel,
        })
        assert generated.status_code == 200, generated.text
        calls = [item for item in generated.json()["output"] if item["type"] == "tool_call"]
        assert len(calls) == (2 if expected_parallel else 1)


@pytest.mark.parametrize("profile,confirmed,rejected", [
    ({}, "high", "medium"),
    ({"reasoning_effort_map": {"low": "low", "ultra": "max", "xhigh": "xhigh"}}, "ultra", "xhigh"),
])
def test_registration_probes_reasoning_for_custom_profiles_and_publishes_only_confirmed_levels(
    tmp_path, monkeypatch, profile, confirmed, rejected,
):
    seen = []
    effort_map = profile.get("reasoning_effort_map", {
        effort: effort for effort in ("minimal", "low", "medium", "high", "xhigh", "max")
    })
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            if "reasoning_effort" in body:
                seen.append(body["reasoning_effort"])
                if body["reasoning_effort"] == effort_map[rejected]:
                    return httpx.Response(400, json={"error": "Unsupported reasoning effort"})
                if body["reasoning_effort"] == effort_map[confirmed]:
                    raw = chat_response("323")
                    raw["choices"][0]["message"]["reasoning_content"] = "17 * 19 = 323"
                    return httpx.Response(200, json=raw)
        return capable_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        assert mutate(client, "/upstreams/local", {"profile": profile}, method="PATCH").status_code == 200
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        assert seen == list(effort_map.values())
        caps = state(client)["models"][0]["declaration"]
        assert caps["reasoning"]["supported"] is True
        assert caps["reasoning"]["efforts"] == [confirmed]
        assert caps["reasoning"]["returns"] == ["none", "content", "auto"]
        checks = response.json()["detection"][0]["checks"]
        assert checks["reasoning." + confirmed]["status"] == "supported"
        assert checks["reasoning." + rejected]["status"] == "unsupported"
        assert checks["reasoning.low"]["status"] == "unknown"
        token = mutate(client, "/keys", {"name": "agent"}, key=True).json()["token"]
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}
        published = client.get("/model/models/detected/capabilities", headers=headers)
        assert published.status_code == 200
        assert published.json()["reasoning"]["efforts"] == [confirmed]
        generated = client.post("/model/responses", headers={**headers, "Idempotency-Key": "req_reasoning"}, json={
            "protocol_version": "2.0", "request_id": "req_reasoning", "model": "detected", "stream": False,
            "input": [{"type": "message", "id": "msg_user", "role": "user",
                       "content": [{"type": "text", "text": "17 times 19"}]}],
            "reasoning": {"enabled": True, "effort": confirmed, "return": "auto"},
        })
        assert generated.status_code == 200, generated.text
        assert generated.json()["output"][0]["type"] == "reasoning"
        assert seen[-1] == effort_map[confirmed]
        invalid = mutate(client, "/models/detected", {"capabilities": {
            "reasoning": {"supported": True, "efforts": ["unmapped"]},
        }}, method="PATCH")
        assert invalid.status_code == 422


def test_deepseek_template_registration_keeps_declared_metadata_separate_from_probe_evidence(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{
                "id": "remote", "context_window": 1048576,
                "effort": {"supported_levels": ["low", "high", "max"]},
            }]})
        body = json.loads(request.content)
        if "reasoning_effort" in body:
            assert body["thinking"] == {"type": "enabled"}
            if body["reasoning_effort"] == "max":
                return httpx.Response(400, json={"error": "Unavailable"})
        return capable_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        response = mutate(client, "/upstreams", {
            "id": "local", "name": "DeepSeek", "template_id": "deepseek",
            "keys": [{"id": "primary", "value": "offline-key"}],
        })
        assert response.status_code == 200, response.text
        assert mutate(client, "/upstreams/local", {"profile": {
            "thinking_mode": True,
            "reasoning_effort_map": {"high": "high", "low": "low", "max": "max"},
        }}, method="PATCH").status_code == 200
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        caps = state(client)["models"][0]["declaration"]
        assert caps["reasoning"]["efforts"] == ["low", "high"]
        assert caps["limits"]["context_window"] == 1048576
        checks = response.json()["detection"][0]["checks"]
        assert checks["reasoning.low"] == {"status": "declared", "reason": "upstream-metadata"}
        assert checks["reasoning.max"]["status"] == "unsupported"


@pytest.mark.parametrize("efforts", [
    ["low", "medium", "high", "xhigh", "max"],
    ["high", "low", "max", "medium", "xhigh"],
])
def test_command_code_preset_registration_probes_all_mapped_levels(tmp_path, monkeypatch, efforts):
    seen = []
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            if "reasoning_effort" in body:
                seen.append(body["reasoning_effort"])
                raw = chat_response("323")
                raw["usage"]["completion_tokens_details"] = {"reasoning_tokens": 1}
                return httpx.Response(200, json=raw)
        return capable_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        response = mutate(client, "/upstreams", {
            "id": "local", "name": "Command", "template_id": "command-code",
            "keys": [{"id": "primary", "value": "offline-key"}],
        })
        assert response.status_code == 200, response.text
        assert mutate(client, "/upstreams/local", {"profile": {
            "reasoning_effort_map": {effort: effort for effort in efforts},
        }}, method="PATCH").status_code == 200
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        assert seen == efforts
        assert state(client)["models"][0]["declaration"]["reasoning"]["efforts"] == [
            "low", "medium", "high", "xhigh", "max",
        ]
        deployment = state(client)["deployments"][0]
        response = mutate(client, "/deployments/" + deployment["id"] + "/probe")
        assert response.status_code == 200, response.text
        assert response.json()["capabilities"]["reasoning"]["efforts"] == [
            "low", "medium", "high", "xhigh", "max",
        ]


def test_new_deployment_without_confirmed_reasoning_cannot_weaken_existing_model(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch, capable_upstream) as (client, _):
        add_upstream(client, identity="first")
        register(client, identity="first", model="shared", capabilities={
            "reasoning": {"supported": True, "efforts": ["high"]},
        })
        add_upstream(client)
        before = state(client)
        response = automatic_registration(client, model="shared")
        assert response.status_code == 422
        after = state(client)
        assert before["revision"] == after["revision"]
        assert before["models"] == after["models"] and before["deployments"] == after["deployments"]


def test_responses_registration_publishes_native_reasoning_capabilities(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "remote"}]})
        body = json.loads(request.content)
        raw = {
            "id": "remote", "status": "completed", "output": [{
                "type": "message", "id": "msg_remote", "content": [{"type": "output_text", "text": "OK"}],
            }],
        }
        if body.get("reasoning", {}).get("effort") == "high":
            assert body["reasoning"]["summary"] == "auto"
            raw["usage"] = {"input_tokens": 3, "output_tokens": 5,
                            "output_tokens_details": {"reasoning_tokens": 2}}
        if body["stream"]:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps({"type": "response.completed", "response": raw}) + "\n\n")
        return httpx.Response(200, json=raw)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-responses")
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        reasoning = state(client)["models"][0]["declaration"]["reasoning"]
        assert reasoning["supported"] is True and reasoning["efforts"] == ["high"]
        assert reasoning["summary"] is True and reasoning["returns"] == ["none", "summary", "auto"]


def test_failed_text_probe_registers_explicit_unknown_without_leaking_error_body(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": "private provider error and credentials"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        model = state(client)["models"][0]
        assert model["verification"] == "unknown"
        assert model["verified_at"] is None
        assert response.json()["detection"][0]["checks"]["text"]["reason"] == "RATE_LIMITED"
        assert response.json()["detection"][0]["checks"]["audio"]["reason"] == "basic-test-failed"
        assert "private provider error" not in json.dumps(state(client))
        assert "private provider error" not in response.text
        assert state(client)["attempts"][0]["status"] == "failed"
        assert len(state(client)["attempts"]) == 1


def test_stale_or_duplicate_registration_never_calls_upstream(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return capable_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        stale = state(client)["revision"]
        add_upstream(client)
        assert automatic_registration(client, expected=stale).status_code == 409
        assert calls == []
        assert automatic_registration(client).status_code == 200
        count = len(calls)
        assert automatic_registration(client).status_code == 422
        assert len(calls) == count


def test_automatic_embedding_and_rerank_registration(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.2, 0.4, 0.8]}]})
        return httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1},
        ]})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        for adapter, task in (("openai-embedding", "embedding"), ("cohere-rerank", "rerank")):
            add_upstream(client, identity=task, adapter=adapter)
            response = mutate(client, f"/upstreams/{task}/register", {"models": [{
                "upstream_model": "remote", "public_model": task,
            }]})
            assert response.status_code == 200, response.text
        models = {m["id"]: m for m in state(client)["models"]}
        assert models["embedding"]["capabilities"]["embedding"]["default_dimensions"] == 3
        assert models["rerank"]["capabilities"]["rerank"]["max_documents"] == 2
        assert all(model["verification"] == "verified" for model in models.values())


def test_detected_candidate_cannot_break_existing_public_contract(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(400, json={"error": "not supported"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, identity="first")
        register(client, identity="first", model="shared", capabilities={"streaming": True})
        add_upstream(client)
        before = state(client)
        response = automatic_registration(client, model="shared")
        assert response.status_code == 422
        after = state(client)
        assert before["revision"] == after["revision"]
        assert before["models"] == after["models"]
        assert before["deployments"] == after["deployments"]


def test_gemini_token_metadata_and_adapter_capability_boundary(tmp_path, monkeypatch):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json={"name": "models/remote", "inputTokenLimit": 32000,
                                           "outputTokenLimit": 2048})
        if "generateContent" in request.url.path:
            body = json.loads(request.content)
            if body.get("tools") or body.get("generationConfig", {}).get("responseMimeType") or any(
                "inlineData" in part for content in body.get("contents", []) for part in content.get("parts", [])
            ):
                return httpx.Response(400, json={"error": "not supported"})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]},
                                                           "finishReason": "STOP"}]})
        return httpx.Response(400, json={"error": "streaming not supported"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="gemini", auth_scheme="api-key")
        response = automatic_registration(client)
        assert response.status_code == 200, response.text
        caps = state(client)["models"][0]["declaration"]
        assert caps["limits"]["max_input_tokens"] == 32000
        assert caps["limits"]["max_output_tokens"] == 2048
        assert caps["limits"]["context_window"] is None
        assert caps["tools"]["function_calling"] is False
        assert caps["streaming"] is False
        assert paths[-1].endswith("/models/remote")
