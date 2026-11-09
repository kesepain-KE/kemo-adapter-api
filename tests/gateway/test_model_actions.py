from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from tests.support.gateway import add_upstream, default_upstream, instance, mutate, register, state


def test_model_test_uses_selected_deployment_and_probe_only_previews(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.content and json.loads(request.content).get("stream"):
            return httpx.Response(400, json={"error": "unsupported"})
        return default_upstream(request)

    with instance(tmp_path, monkeypatch, handler) as (client, app):
        for identity in ("first", "second"):
            add_upstream(client, identity=identity)
            register(client, identity=identity, model="public-model")
        before = state(client)
        deployment = next(d for d in before["deployments"] if d["upstream_id"] == "second")
        path = "/deployments/" + deployment["id"]
        response = mutate(client, path + "/test")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "supported"
        assert result["model"] == "public-model" and result["deployment_id"] == deployment["id"]
        assert result["preview"] == "hello" and result["latency_ms"] >= 0
        assert result["usage"]["total_tokens"] == 5
        assert len(calls) == 1 and calls[0].url.path == "/second/chat/completions"
        assert len(state(client)["attempts"]) == 1

        response = mutate(client, path + "/probe")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["checks"]["text"]["status"] == "supported"
        assert result["checks"]["tools"]["status"] == "unknown"
        assert result["checks"]["audio"] == {
            "status": "unknown", "reason": "response-not-confirmed", "sample_id": "audio-code-v1",
        }
        assert result["checks"]["file"]["reason"] == "response-not-confirmed"
        assert result["checks"]["video"]["reason"] == "adapter-not-implemented"
        assert result["checks"]["output.audio"]["status"] == "unsupported"
        assert result["capabilities"]["input_modalities"] == ["text"]
        assert len(state(client)["attempts"]) == 14  # Existing checks plus six reasoning candidates.
        assert all(result["checks"]["reasoning." + effort]["status"] == "unknown"
                   for effort in ("minimal", "low", "medium", "high", "xhigh", "max"))
        after = state(client)
        for key in ("revision", "models", "deployments", "upstreams", "settings"):
            assert before[key] == after[key]
        assert app.state.registry._references.get(id(app.state.registry.current), 0) == 0


@pytest.mark.parametrize("operation", ["test", "probe"])
def test_model_actions_reject_stale_disabled_missing_or_unexpected_input_before_call(tmp_path, monkeypatch, operation):
    calls = []
    def handler(request):
        calls.append(request)
        return default_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        before = state(client)
        deployment = before["deployments"][0]["id"]
        path = f"/deployments/{deployment}/{operation}"
        assert mutate(client, path, expected="stale").status_code == 409
        assert mutate(client, f"/deployments/missing/{operation}").status_code == 404
        assert mutate(client, path, {"capabilities": {}}).status_code == 422
        assert mutate(client, f"/deployments/{deployment}", {"enabled": False}, method="PATCH").status_code == 200
        assert mutate(client, path).status_code == 422
        assert calls == [] and state(client)["attempts"] == []


def test_failed_test_returns_safe_reason_without_changing_model(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": "private credentials in upstream body"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        before = state(client)
        deployment = before["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/test")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unknown"
        assert response.json()["reason"] == "RATE_LIMITED"
        assert response.json()["provider_status"] == 429
        assert "private credentials" not in response.text
        after = state(client)
        assert before["models"] == after["models"] and before["revision"] == after["revision"]
        assert after["attempts"][0]["status"] == "failed"


@pytest.mark.parametrize(("adapter", "task", "field", "expected"), [
    ("openai-embedding", "embedding", "dimensions", 3),
    ("cohere-rerank", "rerank", "result_count", 1),
])
@pytest.mark.parametrize("first_token", [False, True])
def test_model_test_supports_retrieval_tasks(tmp_path, monkeypatch, adapter, task, field, expected, first_token):
    calls = []
    def handler(request):
        calls.append(request)
        if task == "embedding":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter=adapter)
        register(client, task=task, capabilities={"streaming": False, "output_modalities": [],
            **({"embedding": {"input_types": ["document"], "default_dimensions": 3,
                              "supported_dimensions": [3], "max_batch_size": 1}}
               if task == "embedding" else {"rerank": {"max_documents": 2}})})
        deployment = state(client)["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/test" + ("?first_token=true" if first_token else ""))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "supported"
        assert response.json()[field] == expected and len(calls) == 1
        if first_token:
            assert response.json()["first_token_ms"] is None
            assert response.json()["streaming"] is False


def test_model_test_keeps_generation_alive_during_concurrent_publish(tmp_path, monkeypatch):
    registry = None
    leased = None
    async def handler(request):
        assert not leased.transport.client.is_closed
        current = registry.store.catalog()
        await registry.publish(current.model_copy(update={
            "settings": current.settings.model_copy(update={"system_prompt": "published during test"}),
        }), registry.current.revision, "concurrent-test")
        assert leased in registry._retired.values()
        assert not leased.transport.client.is_closed
        return default_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, app):
        registry = app.state.registry
        add_upstream(client)
        register(client)
        leased = registry.current
        deployment = state(client)["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/test")
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == leased.revision
        assert response.json()["revision"] != registry.current.revision
        assert leased.transport.client.is_closed
        assert registry._references == {} and registry._retired == {}


def test_first_token_is_first_text_not_role_reasoning_usage_or_completion(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("gateway.model_probe.time", SimpleNamespace(perf_counter=lambda: clock[0]))
    calls, closed = [], []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            frames = [
                (0.05, {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}),
                (0.2, {"choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": None}]}),
                (0.3, {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 0}}),
                (0.45, {"choices": [{"delta": {"content": "he"}, "finish_reason": None}]}),
                (0.8, {"choices": [{"delta": {"content": "llo"}, "finish_reason": None}]}),
                (1.0, {"choices": [{"delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 3, "completion_tokens": 2}}),
            ]
            for moment, frame in frames:
                clock[0] = moment
                yield ("data: " + json.dumps(frame) + "\n\n").encode()
        async def aclose(self):
            closed.append(True)

    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Stream())
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        before = state(client)
        deployment = before["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/test?first_token=true")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "supported" and result["streaming"] is True
        assert result["first_token_ms"] == 450
        assert result["latency_ms"] == 1000
        assert result["preview"] == "hello" and result["usage"]["total_tokens"] == 5
        assert len(calls) == 1 and closed == [True]
        after = state(client)
        assert len(after["attempts"]) == 1 and after["attempts"][0]["status"] == "completed"
        assert after["attempts"][0]["source"] == "exact"
        for key in ("revision", "models", "deployments", "upstreams", "settings"):
            assert before[key] == after[key]


@pytest.mark.parametrize("failure", ["no-text", "rate-limit"])
def test_first_token_remains_unknown_until_text_arrives(tmp_path, monkeypatch, failure):
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        if failure == "rate-limit":
            return httpx.Response(429, json={"error": "private upstream error"})
        frame = {"choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="data: " + json.dumps(frame) + "\n\n")
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        deployment = state(client)["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/test?first_token=true")
        assert response.status_code == 200, response.text
        assert response.json()["status"] != "supported"
        assert response.json()["first_token_ms"] is None
        assert response.json()["streaming"] is True
        assert "private upstream error" not in response.text
        assert len(state(client)["attempts"]) == 1


def test_nonstreaming_adapter_returns_response_time_without_inventing_first_token(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, json={"success": True, "result": {"response": "OK"}})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="cloudflare-workers-ai")
        register(client, capabilities={"streaming": False})
        deployment = state(client)["deployments"][0]["id"]
        response = mutate(client, f"/deployments/{deployment}/test?first_token=true")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "supported" and result["preview"] == "OK"
        assert result["first_token_ms"] is None and result["streaming"] is False
        assert result["latency_ms"] >= 0 and len(calls) == 1
