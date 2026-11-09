from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import zipfile

import httpx
import pytest

from api.server import create_app
from core.config import Settings
from core.models import KemoRequest
from gateway.domain import Catalog, Deployment, PublicModel, Upstream
from gateway.runtime import RuntimeRegistry
from gateway.security import SecretVault
from gateway.store import Store, dump, revision
from gateway.transport import Transport, safe_addresses
from tests.support.retrieval import embedding_body, rerank_body
from tests.support.gateway import (
    SECRET, add_upstream, chat_response, default_upstream, instance, mutate, register, state,
    key, invoke, extension_zip, install_extension,
)






def test_registration_invocation_idempotency_and_encrypted_storage(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return default_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, app):
        add_upstream(client)
        discovered = client.post("/admin/api/upstreams/local/discover")
        assert discovered.json()["models"] == ["remote"]
        register(client)
        created = key(client)
        token = created["token"]
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}
        assert client.get("/model/models", headers=headers).json()["data"][0]["id"] == "stable"
        response = invoke(client, token)
        assert response.status_code == 200, response.text
        assert response.json()["output"][0]["content"][0]["text"] == "hello"
        assert response.json()["output"][0]["phase"] == "final_answer"
        assert response.json()["usage"]["total_tokens"] == 5
        assert invoke(client, token).json()["id"] == response.json()["id"]
        assert len(calls) == 2  # discovery and one billable attempt
        logs = client.get("/admin/api/logs").json()["items"]
        assert len(logs) == 1 and logs[0]["source"] == "exact"
        assert SECRET not in json.dumps(state(client))
        assert token not in json.dumps(state(client))
        assert SECRET.encode() not in app.state.registry.store.path.read_bytes()
        assert token.encode() not in app.state.registry.store.path.read_bytes()
        assert not (tmp_path / "storage").exists()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("is_error", [False, True])
def test_json_tool_receipt_continues_through_public_gateway(tmp_path, monkeypatch, stream, is_error):
    value = "工具执行失败：fixture error" if is_error else "TOOL_OUTPUT_TEST_12345"
    receipt = {
        "execution_id": "exec_offline",
        "execution": {"status": "failed" if is_error else "completed", "execution_live": False},
        "original_call": {"name": "echo", "arguments": {"value": "TOOL_OUTPUT_TEST_12345"}},
        "result": {"ok": False, "error": value} if is_error else {"ok": True, "result": {"value": value}},
    }
    seen = []

    def handler(http_request):
        body = json.loads(http_request.content)
        results = [message for message in body["messages"] if message["role"] == "tool"]
        if not results:
            content = {"tool_calls": [{
                "id": "remote-call", "type": "function",
                "function": {"name": "echo", "arguments": '{"value":"TOOL_OUTPUT_TEST_12345"}'},
            }]}
            reason = "tool_calls"
        else:
            assert len(results) == 1 and results[0]["tool_call_id"] == call["call_id"]
            received = json.loads(results[0]["content"])
            assert received == receipt
            content = {"content": received["result"]["error"] if is_error else received["result"]["result"]["value"]}
            reason = "stop"
        seen.append(body)
        if not stream:
            return httpx.Response(200, json={
                "id": "remote", "choices": [{"index": 0, "message": content, "finish_reason": reason}],
            })
        if "tool_calls" in content:
            content["tool_calls"][0]["index"] = 0
        frames = [
            {"id": "remote", "choices": [{"index": 0, "delta": content}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        ]
        return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
                              headers={"content-type": "text/event-stream"})

    def invoke_native(client, token, body):
        response = client.post("/model/responses", headers={
            "Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": body["request_id"],
        }, json=body)
        assert response.status_code == 200, response.text
        if not stream:
            return response.json()
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "response.completed", events[-1]
        return events[-1]["response"]

    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client, capabilities={"streaming": True, "tools": {"function_calling": True}})
        token = key(client)["token"]
        body = {"protocol_version": "2.0", "request_id": "req_tool_first", "model": "stable",
                "stream": stream, "parallel_tool_calls": False, "tools": [{
                    "name": "echo", "description": "Return the supplied value",
                    "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                   "required": ["value"]},
                }], "input": [{"type": "message", "id": "msg_user", "role": "user",
                              "content": [{"type": "text", "text": "echo the value"}]}]}
        first = invoke_native(client, token, body)
        assert first["status"] == "requires_action"
        call = first["output"][0]
        body["request_id"] = "req_tool_continue"
        body["input"].extend([call, {
            "type": "tool_result", "id": "result_echo", "call_id": call["call_id"],
            "name": "echo", "is_error": is_error, "content": [{"type": "json", "data": receipt}],
        }])
        final = invoke_native(client, token, body)
        assert final["status"] == "completed"
        assert final["output"][0]["phase"] == "final_answer"
        assert final["output"][0]["content"][0]["text"] == value
        assert len(seen) == 2


def test_invalid_candidate_and_stale_revision_leave_runtime_intact(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        original = state(client)["revision"]
        register(client)
        active = app.state.registry.current
        response = mutate(client, "/settings", {"enabled": False}, method="PATCH", expected=original)
        assert response.status_code == 409
        assert app.state.registry.current is active
        response = mutate(client, "/models/stable", {"capabilities": {"output_modalities": ["video"]}}, method="PATCH")
        assert response.status_code == 422
        assert app.state.registry.current is active
        assert app.state.registry.store.catalog().models[0].id == "stable"


@pytest.mark.parametrize("efforts,expected", [
    (["high", "low", "max", "medium", "xhigh", "minimal"],
     ["minimal", "low", "medium", "high", "xhigh", "max"]),
    (["vendor-z", "high", "vendor-a", "low"], ["low", "high", "vendor-z", "vendor-a"]),
])
def test_reasoning_order_for_manual_and_existing_models(tmp_path, monkeypatch, efforts, expected):
    with instance(tmp_path, monkeypatch) as (client, app):
        add_upstream(client)
        assert mutate(client, "/upstreams/local", {"profile": {
            "reasoning_effort_map": {effort: effort for effort in efforts},
        }}, method="PATCH").status_code == 200
        register(client, capabilities={"reasoning": {"supported": True, "efforts": efforts}})
        assert state(client)["models"][0]["capabilities"]["reasoning"]["efforts"] == expected
        token = key(client)["token"]
        legacy = app.state.registry.store.catalog().models[0].model_dump(mode="json")
        legacy.update(verification="verified", evidence="existing-probe-evidence", verified_at="2026-11-01T00:00:00+00:00")
        legacy["capabilities"]["reasoning"]["efforts"] = efforts
        with app.state.registry.store.transaction() as db:
            db.execute("UPDATE models SET body=? WHERE id=?", (dump(legacy), "stable"))

    with instance(tmp_path, monkeypatch) as (client, app):
        model = state(client)["models"][0]
        assert model["capabilities"]["reasoning"]["efforts"] == expected
        assert model["declaration"]["reasoning"]["efforts"] == expected
        assert model["verification"] == "verified"
        assert model["evidence"] == legacy["evidence"] and model["verified_at"] == legacy["verified_at"]
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}
        for path in ("/model/capabilities?model=stable", "/model/models/stable/capabilities"):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["reasoning"]["efforts"] == expected
        # A loaded legacy catalog must still accept edits using the advertised revision.
        response = mutate(client, "/models/stable", {"display_name": "Renamed"}, method="PATCH")
        assert response.status_code == 200, response.text
        assert state(client)["models"][0]["verification"] == "verified"
        assert state(client)["attempts"] == []


def test_deployment_failover_records_each_attempt(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.startswith("/primary"):
            return httpx.Response(503, json={"error": "upstream failed"})
        return default_upstream(request)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, "primary")
        add_upstream(client, "backup")
        register(client, "primary")
        register(client, "backup")
        first = next(d for d in state(client)["deployments"] if d["upstream_id"] == "primary")
        assert mutate(client, f"/deployments/{first['id']}", {"priority": 10}, method="PATCH").status_code == 200
        response = invoke(client, key(client)["token"])
        assert response.status_code == 200, response.text
        logs = client.get("/admin/api/logs").json()["items"]
        assert {log["status"] for log in logs} == {"failed", "completed"}
        assert len(calls) == 2 and calls[0].startswith("/primary")


def test_stream_persists_unique_terminal_and_replays_without_upstream(tmp_path, monkeypatch):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        frames = [
            {"id": "remote-stream", "choices": [{"index": 0, "delta": {"content": "he"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        ]
        return httpx.Response(200, text="".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client)
        register(client)
        token = key(client)["token"]
        response = invoke(client, token, stream=True)
        assert response.status_code == 200, response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert len([e for e in events if e["type"] == "response.completed"]) == 1
        terminal = next(e["response"] for e in events if e["type"] == "response.completed")
        assert terminal["status"] == "completed"
        assert terminal["output"][0]["phase"] == "final_answer"
        assert [e["sequence"] for e in events] == list(range(len(events)))
        assert "output_text.done" in [e["type"] for e in events]
        replay = invoke(client, token, stream=True)
        assert replay.text == response.text
        assert count == 1


@pytest.mark.parametrize("adapter", ["openai-chat", "cohere-chat"])
@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_and_text_preserve_protocol_and_completed_attempt(tmp_path, monkeypatch, adapter, stream):
    def handler(request):
        if adapter == "openai-chat":
            if not stream:
                raw = chat_response()
                raw["choices"][0]["message"]["reasoning_content"] = "think"
                return httpx.Response(200, json=raw)
            frames = [
                {"id": "remote", "choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]},
                {"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            ]
        else:
            usage = {"billed_units": {"input_tokens": 3, "output_tokens": 2}}
            if not stream:
                return httpx.Response(200, json={
                    "id": "remote", "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "think"}, {"type": "text", "text": "hello"},
                    ]}, "finish_reason": "COMPLETE", "usage": usage,
                })
            frames = [
                {"type": "message-start", "id": "remote"},
                {"type": "content-delta", "delta": {"message": {"content": {"thinking": "think"}}}},
                {"type": "content-delta", "delta": {"message": {"content": {"text": "hello"}}}},
                {"type": "message-end", "delta": {"finish_reason": "COMPLETE", "usage": usage}},
            ]
        return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
                              headers={"content-type": "text/event-stream"})

    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter=adapter)
        register(client)
        response = invoke(client, key(client)["token"], stream=stream)
        assert response.status_code == 200, response.text
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert [event["type"] for event in events] == [
                "response.created", "output_item.added", "reasoning.content.delta",
                "output_item.added", "output_text.delta", "output_text.done",
                "usage.updated", "response.completed",
            ]
            assert events[2]["delta"] == "think" and "item" not in events[2]
            assert events[4]["delta"] == "hello"
            result = events[-1]["response"]
            assert result["output"][0]["id"] == events[1]["item_id"] == events[2]["item_id"]
            assert result["output"][1]["id"] == events[3]["item_id"] == events[4]["item_id"]
        else:
            result = response.json()
        assert result["status"] == "completed"
        assert result["output"][0]["type"] == "reasoning"
        assert result["output"][0]["content"] == "think"
        assert result["output"][1]["content"][0]["text"] == "hello"
        assert result["output"][1]["phase"] == "final_answer"
        assert result["usage"]["total_tokens"] == 5
        logs = client.get("/admin/api/logs").json()["items"]
        assert len(logs) == 1 and logs[0]["status"] == "completed"
        assert logs[0]["error_code"] is None


def test_stream_failure_does_not_fail_over(tmp_path, monkeypatch):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, "one")
        add_upstream(client, "two")
        register(client, "one")
        register(client, "two")
        response = invoke(client, key(client)["token"], stream=True)
        assert count == 1
        assert "response.failed" in response.text
        assert len(client.get("/admin/api/logs").json()["items"]) == 1


def test_key_revocation_model_policy_and_rpm_are_server_enforced(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        add_upstream(client)
        register(client)
        created = key(client, rpm=1)
        assert invoke(client, created["token"]).status_code == 200
        limited = invoke(client, created["token"], "req_two")
        assert limited.status_code == 200
        assert limited.json()["status"] == "failed"
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert len(client.get("/admin/api/logs").json()["items"]) == 1
        denied = key(client, allowed_models=[])
        assert invoke(client, denied["token"], "req_denied").status_code == 403
        assert mutate(client, f"/keys/{created['id']}", {"enabled": False}, method="PATCH", key=True).status_code == 200
        assert invoke(client, created["token"], "req_revoked").status_code == 401


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:8000/v1", False),
    ("http://[::1]:8000/v1", False),
    ("http://169.254.169.254/latest", True),
    ("http://100.100.100.200", True),
    ("http://user:password@example.com", True),
    ("http://127.0.0.1/?api_key=secret", True),
])
def test_ssrf_rejects_unsafe_addresses(url, allowed):
    with pytest.raises(ValueError):
        asyncio.run(safe_addresses(url, allowed))


@pytest.mark.parametrize(("url", "address"), [
    ("http://127.0.0.1:8000/v1", "127.0.0.1"),
    ("http://[::1]:8000/v1", "::1"),
])
def test_local_network_exception_is_explicit(url, address):
    assert asyncio.run(safe_addresses(url, True)) == (address,)


def test_generation_lease_retains_old_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("KEMO_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    async def scenario():
        store = Store(tmp_path / "data")
        registry = RuntimeRegistry(tmp_path, store, SecretVault(tmp_path / "data"))
        await registry.initialize()
        first = Catalog(upstreams=(Upstream(id="local", name="local", adapter_id="openai-chat",
                                           base_url="http://127.0.0.1", auth_scheme="none", allow_private_network=True),),
                        models=(PublicModel(id="stable"),),
                        deployments=(Deployment(id="d", upstream_id="local", upstream_model="remote", public_model="stable"),))
        await registry.publish(first, registry.current.revision, "test")
        lease = registry.acquire_active("stable")
        old = registry.current
        next_catalog = first.model_copy(update={"settings": first.settings.model_copy(update={"system_prompt": "new"})})
        await registry.publish(next_catalog, old.revision, "test")
        assert not old.transport.client.is_closed
        assert lease.generation is old
        assert registry.resolve("stable").generation is registry.current
        await registry.release_registered(lease)
        assert old.transport.client.is_closed
        await registry.close()
        store.close()
    asyncio.run(scenario())






def test_extension_worker_invocation_and_rejected_replacement(tmp_path, monkeypatch):
    import os
    code = """
import os
from core.provider_contract import ProviderResult
class Adapter:
    async def execute(self, request, context, model, upstream, headers):
        return ProviderResult("completed", output=[{
            "type": "message", "id": "msg_worker", "role": "assistant",
            "content": [{"type": "text", "text": str(os.getpid())}]}])
def create_adapter(transport):
    return Adapter()
"""
    with instance(tmp_path, monkeypatch) as (client, app):
        content = extension_zip("protocol_adapters", code=code)
        assert install_extension(client, "protocol_adapters", content).status_code == 200
        assert state(client)["adapters"][-1]["enabled"] is False
        assert mutate(client, "/extensions/protocol_adapters/echo", {"enabled": True}, method="PATCH").status_code == 200
        add_upstream(client, adapter="echo", auth_scheme="none")
        register(client, capabilities={"streaming": False})
        token = key(client)["token"]
        response = invoke(client, token)
        assert response.status_code == 200, response.text
        assert int(response.json()["output"][0]["content"][0]["text"]) != os.getpid()
        assert response.json()["usage"]["measurement"]["mode"] == "unknown"
        active = app.state.registry.current
        (tmp_path / "extensions/protocol_adapters/echo/adapter.py").write_text(
            "raise RuntimeError('broken replacement')", encoding="utf-8")
        rejected = mutate(client, "/extensions/protocol_adapters/echo", {"enabled": True}, method="PATCH")
        assert rejected.status_code == 502
        assert app.state.registry.current is active
        assert invoke(client, token, "req_after_rejection").json()["status"] == "completed"


def test_template_defaults_registration_and_reference_integrity(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        content = extension_zip("provider_templates", template={
            "adapter_id": "openai-chat", "name": "From template", "auth_scheme": "none",
            "base_url": "http://127.0.0.1:8881", "catalog": ["remote"],
        })
        assert install_extension(client, "provider_templates", content).status_code == 200
        assert mutate(client, "/extensions/provider_templates/echo", {"enabled": True}, method="PATCH").status_code == 200
        result = mutate(client, "/upstreams", {"id": "templated", "template_id": "echo", "allow_private_network": True})
        assert result.status_code == 200, result.text
        assert result.json()["upstream"]["name"] == "From template"
        active = app.state.registry.current
        rejected = mutate(client, "/extensions/provider_templates/echo", {"enabled": False}, method="PATCH")
        assert rejected.status_code == 422
        assert app.state.registry.current is active
        register(client, identity="templated")
        assert invoke(client, key(client)["token"]).json()["status"] == "completed"


def test_builtin_templates_are_visible_and_can_create_upstreams_without_install(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, app):
        templates = {item["id"]: item for item in state(client)["templates"]}
        assert len(templates) == 46
        assert {"openai", "anthropic", "gemini", "openai-responses"} <= templates.keys()
        assert all(item["builtin"] and item["enabled"] and item["valid"] for item in templates.values())
        assert not (tmp_path / "extensions").exists()
        for identity, adapter, auth in (
            ("openai", "openai-chat", "bearer"),
            ("anthropic", "anthropic-messages", "anthropic-key"),
            ("gemini", "gemini", "gemini-query"),
            ("cohere", "cohere-chat", "bearer"),
        ):
            response = mutate(client, "/upstreams", {
                "id": identity, "template_id": identity,
                "base_url": "http://127.0.0.1:8881", "allow_private_network": True,
            })
            assert response.status_code == 200, response.text
            upstream = response.json()["upstream"]
            assert upstream["adapter_id"] == adapter
            assert upstream["auth_scheme"] == auth
            assert upstream["name"] == templates[identity]["template"]["name"]
        assert app.state.registry.current.upstreams["openai"].profile["max_tokens_field"] == "max_completion_tokens"
        assert len(app.state.registry.current.templates) == 46
        assert len(state(client)["templates"]) == 46


@pytest.mark.parametrize("enabled", [False, True])
def test_installed_template_overrides_builtin_only_when_enabled(tmp_path, monkeypatch, enabled):
    with instance(tmp_path, monkeypatch) as (client, _):
        content = extension_zip("provider_templates", identity="openai", template={
            "adapter_id": "openai-chat", "name": "Custom OpenAI", "auth_scheme": "none",
            "base_url": "http://127.0.0.1:8881",
        })
        assert install_extension(client, "provider_templates", content).status_code == 200
        if enabled:
            response = mutate(client, "/extensions/provider_templates/openai", {"enabled": True}, method="PATCH")
            assert response.status_code == 200, response.text
        choices = [item for item in state(client)["templates"] if item["id"] == "openai" and item["enabled"]]
        assert len(choices) == 1
        assert bool(choices[0].get("builtin")) is not enabled
        response = mutate(client, "/upstreams", {
            "id": "templated", "template_id": "openai",
            "base_url": "http://127.0.0.1:8881", "allow_private_network": True,
        })
        assert response.status_code == 200, response.text
        assert response.json()["upstream"]["name"] == ("Custom OpenAI" if enabled else "OpenAI")
        if enabled:
            response = mutate(client, "/extensions/provider_templates/openai", {"enabled": False}, method="PATCH")
            assert response.status_code == 200, response.text
            choices = [item for item in state(client)["templates"] if item["id"] == "openai" and item["enabled"]]
            assert len(choices) == 1 and choices[0]["builtin"]


@pytest.mark.parametrize("name", ["../adapter.py", "/adapter.py", "C:/adapter.py", "nested/adapter.py"])
def test_extension_archive_rejects_unsafe_paths(tmp_path, monkeypatch, name):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, "not executed")
    with instance(tmp_path, monkeypatch) as (client, _):
        response = install_extension(client, "protocol_adapters", output.getvalue())
        assert response.status_code == 422


def test_budget_reservation_settlement_and_rejection(tmp_path, monkeypatch):
    with instance(tmp_path, monkeypatch) as (client, _):
        add_upstream(client)
        register(client, capabilities={"limits": {"context_window": 10}})
        deployment = state(client)["deployments"][0]
        assert mutate(client, f"/deployments/{deployment['id']}", {
            "price": {"input_per_million": 1_000_000, "output_per_million": 1_000_000},
        }, method="PATCH").status_code == 200
        token = key(client, budget=34)["token"]
        assert invoke(client, token).json()["status"] == "completed"
        limited = invoke(client, token, "req_budget")
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert len(client.get("/admin/api/logs").json()["items"]) == 1


@pytest.mark.parametrize(("adapter", "body", "expected_input", "expected_output"), [
    ("openai-responses", {
        "id": "up_response", "status": "completed",
        "output": [{"id": "remote_msg", "type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }, 3, 2),
    ("anthropic-messages", {
        "id": "up_response", "type": "message", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1, "output_tokens": 2},
    }, 6, 2),
    ("gemini", {
        "responseId": "up_response", "candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "thoughtsTokenCount": 1},
    }, 3, 3),
])
def test_text_protocols_preserve_usage_semantics(tmp_path, monkeypatch, adapter, body, expected_input, expected_output):
    def handler(request):
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json=body)
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter=adapter, auth_scheme="none")
        register(client)
        response = invoke(client, key(client)["token"])
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed", response.text
        assert result["usage"]["input_tokens"] == expected_input
        assert result["usage"]["output_tokens"] == expected_output
        assert result["usage"]["total_tokens"] == expected_input + expected_output


def test_embedding_adapter_preserves_order_normalization_and_replay(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        assert request.url.path.endswith("/embeddings")
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "data": [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [3.0, 4.0]}],
            "usage": {"prompt_tokens": 7, "total_tokens": 7},
        })
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="openai-embedding", auth_scheme="none")
        register(client, task="embedding", capabilities={
            "streaming": False, "output_modalities": ["embedding"],
            "embedding": {"input_types": ["document"], "default_dimensions": 2,
                          "supported_dimensions": [2], "max_batch_size": 2, "normalization": "always"},
        })
        body = {**embedding_body(), "model": "stable"}
        headers = {"Authorization": "Bearer " + key(client)["token"],
                   "X-Kemo-Protocol-Version": "2.0", "Idempotency-Key": body["request_id"]}
        response = client.post("/model/embeddings", headers=headers, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert [item["id"] for item in result["data"]] == ["node-a", "node-b"]
        assert result["data"][0]["vector"] == pytest.approx([0.6, 0.8])
        assert result["usage"]["total_tokens"] == 7
        assert client.post("/model/embeddings", headers=headers, json=body).json() == result
        assert len(calls) == 1 and calls[0]["input"] == ["alpha", "beta"]
        logs = client.get("/admin/api/logs").json()["items"]
        assert len(logs) == 1 and logs[0]["source"] == "exact"


def test_rerank_adapter_keeps_search_units_separate_from_tokens(tmp_path, monkeypatch):
    def handler(request):
        assert request.url.path.endswith("/rerank")
        return httpx.Response(200, json={
            "id": "up_rerank", "results": [
                {"index": 0, "relevance_score": 0.2}, {"index": 2, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.5},
            ], "meta": {"billed_units": {"search_units": 4}},
        })
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="cohere-rerank", auth_scheme="none")
        register(client, task="rerank", capabilities={
            "streaming": False, "output_modalities": ["score"],
            "rerank": {"max_documents": 10},
        })
        body = {**rerank_body(), "model": "stable"}
        headers = {"Authorization": "Bearer " + key(client)["token"],
                   "X-Kemo-Protocol-Version": "2.0", "Idempotency-Key": body["request_id"]}
        response = client.post("/model/rerank", headers=headers, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert [item["document_id"] for item in result["results"]] == ["doc-c", "doc-b"]
        assert result["results"][0]["document"]["metadata"] == {"node": "c"}
        assert result["usage"]["total_tokens"] is None
        logs = client.get("/admin/api/logs").json()["items"]
        assert len(logs) == 1 and logs[0]["source"] == "unknown" and logs[0]["cost"] is None
