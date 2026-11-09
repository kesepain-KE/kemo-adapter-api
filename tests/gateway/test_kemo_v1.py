"""Archive fixtures validate only the legacy adapter boundary, never core models."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from core import models
from core.provider_contract import ProviderException, RequestContext
from gateway.adapters.builtin.kemo_v1 import KemoV1, item
from gateway.domain import Upstream
from gateway.transport import Transport
from tests.support.gateway import add_upstream, instance, key, register


BUNDLE = json.loads((Path(__file__).parent / "fixtures/kemo_v1_wire.json").read_text(encoding="utf-8"))
CONTEXT = RequestContext("tenant", "agent", "req_local", "resp_local", "trace")
UPSTREAM = Upstream(id="legacy", name="Legacy", adapter_id="kemo-v1", base_url="https://upstream.example")


def document(reference):
    if isinstance(reference, str):
        reference = {"document": reference}
    value = deepcopy(BUNDLE["documents"][reference["document"]])
    for mutation in reference.get("mutations", []):
        parent = value
        for part in mutation["path"][:-1]:
            parent = parent[part]
        if mutation["op"] == "delete":
            del parent[mutation["path"][-1]]
        else:
            parent[mutation["path"][-1]] = deepcopy(mutation["value"])
    return value


def normalized(case, raw):
    adapter = KemoV1(None)
    if case["model"] == "SSEEvent":
        return adapter.event(raw)
    value = adapter.decode({name: entry for name, entry in raw.items() if name != "output"}
                           if case["model"] == "KemoRequest" else raw)
    if case["model"] == "KemoRequest":
        value["output"] = raw["output"]
        value["input"] = [item(entry) for entry in value["input"]]
        value["parallel_tool_calls"] = value["generation"].pop("parallel_tool_calls", True)
    return getattr(models, case["model"]).model_validate(value)


@pytest.mark.parametrize("case", BUNDLE["valid_cases"], ids=lambda case: case["id"])
def test_archived_valid_documents(case):
    converted = normalized(case, document(case))
    assert converted.protocol_version == "2.0"
    if case["model"] == "KemoRequest":
        encoded = KemoV1(None).encode(converted, CONTEXT, converted.model, UPSTREAM)
        assert encoded["protocol_version"] == "1.0"
        assert encoded["generation"]["parallel_tool_calls"] == converted.parallel_tool_calls
        assert "parallel_tool_calls" not in encoded and "tool_choice" not in encoded
        assert all("metadata" in entry and "extensions" in entry for entry in encoded["input"])
        original = document(case)
        assert [entry["id"] for entry in encoded["input"]] == [entry["id"] for entry in original["input"]]
        if converted.parent_request_id:
            assert encoded["parent_request_id"] == KemoV1.request_id(
                converted.parent_request_id, CONTEXT, converted.model, UPSTREAM)
    elif case["document"] == "response_failed":
        assert converted.error.code == "UNKNOWN_ERROR"
        assert converted.error.details["original_code"] == "FIXTURE_FAILURE"
    elif case["document"] == "capability_llm":
        assert "adaptive-high" in converted.reasoning.efforts
        assert "summary" in converted.reasoning.returns
        assert converted.structured_output.supported is False
        assert converted.tools.tool_choice_modes == ["auto"]


@pytest.mark.parametrize("case", BUNDLE["invalid_cases"], ids=lambda case: case["id"])
def test_archived_invalid_documents_are_rejected(case):
    with pytest.raises((ProviderException, ValidationError, ValueError)):
        normalized(case, document(case))


@pytest.mark.parametrize("case", BUNDLE["valid_streams"], ids=lambda case: case["id"])
def test_archived_streams_keep_order_and_deduplicate_replays(case):
    adapter, accepted = KemoV1(None), []
    guard = adapter.stream_guard()
    for reference in case["events"]:
        event = adapter.event(document(reference))
        if guard.accept(event):
            accepted.append(event.sequence)
    assert accepted == case["accepted_sequences"]
    assert guard.terminal


@pytest.mark.parametrize("case", BUNDLE["invalid_streams"], ids=lambda case: case["id"])
def test_archived_broken_streams_are_rejected(case):
    def handler(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        events = [document(reference) for reference in case["events"]]
        for event in events:
            event["request_id"] = body["request_id"]
            if "response" in event:
                event["response"].update(request_id=body["request_id"], model="remote")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
    async def run():
        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            adapter = KemoV1(transport)
            request = models.KemoRequest.create(model="remote", input=[{
                "type": "message", "id": "msg_user", "role": "user", "content": [{"type": "text", "text": "hi"}],
            }])
            with pytest.raises(ProviderException):
                async for _ in adapter.stream(request, CONTEXT, "remote", UPSTREAM, {}):
                    pass
        finally:
            await transport.close()
    asyncio.run(run())


@pytest.mark.parametrize("fields", [
    {"tool_choice": {"mode": "required"}},
    {"structured_output": {"type": "json_schema", "schema_name": "test", "schema": {"type": "object"}}},
    {"generation": {"n": 2}},
    {"generation": {"verbosity": "high"}},
    {"service": {"tier": "priority"}},
    {"prediction": {"type": "content", "content": "expected"}},
])
def test_unsupported_explicit_options_never_silently_disappear(fields):
    request = models.KemoRequest.create(model="remote", stream=False, input=[{
        "type": "message", "id": "msg_user", "role": "user", "content": [{"type": "text", "text": "hello"}],
    }], tools=[{"type": "function", "name": "test", "description": "Test.", "parameters": {"type": "object"}}]
        if "tool_choice" in fields else [],
        **fields)
    with pytest.raises(ProviderException) as raised:
        KemoV1(None).encode(request, CONTEXT, "remote", UPSTREAM)
    assert raised.value.error.code == "CAPABILITY_ERROR"


@pytest.mark.parametrize("stream", [False, True])
def test_legacy_tool_receipt_roundtrip_through_public_v2_api(tmp_path, monkeypatch, stream):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["protocol_version"] == request.headers["X-Kemo-Protocol-Version"] == "1.0"
        assert body["request_id"] == request.headers["Idempotency-Key"]
        if body["input"][-1]["type"] == "tool_result":
            assert body["input"][-1]["call_id"] == body["input"][-2]["call_id"] == "call_response_fixture"
            response = document("response_completed")
        else:
            response = document("response_requires_action")
        response.update(request_id=body["request_id"], model="remote")
        if not body["stream"]:
            return httpx.Response(200, json=response)
        events = [document(reference) for reference in BUNDLE["valid_streams"][1]["events"]]
        for event in events:
            event["request_id"] = body["request_id"]
            if "response" in event:
                event["response"].update(request_id=body["request_id"], model="remote")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
    with instance(tmp_path, monkeypatch, handler) as (client, _):
        add_upstream(client, adapter="kemo-v1")
        register(client, capabilities={
            "streaming": True, "tools": {"function_calling": True, "parallel_calls": True,
                                        "tool_choice_modes": ["auto"]},
            "reasoning": {"supported": True, "efforts": ["adaptive-high"],
                          "returns": ["none", "auto", "summary"], "summary": True},
        })
        token = key(client)["token"]
        tool = document("request_tool_followup")["tools"][0]
        body = {"protocol_version": "2.0", "request_id": "req_tools", "model": "stable", "stream": stream,
                "tools": [tool], "input": [{"type": "message", "id": "msg_user", "role": "user",
                                          "content": [{"type": "text", "text": "lookup"}]}]}
        headers = {"Authorization": "Bearer " + token, "X-Kemo-Protocol-Version": "2.0"}
        response = client.post("/model/responses", headers={**headers, "Idempotency-Key": "req_tools"}, json=body)
        assert response.status_code == 200, response.text
        result = ([json.loads(line[6:]) for line in response.text.splitlines()
                   if line.startswith("data: ")][-1]["response"] if stream else response.json())
        assert result["status"] == "requires_action", result
        call = result["output"][0]
        assert call["id"] == "call_response_fixture" and call["call_id"] == "callid_response_fixture"
        body.update(request_id="req_receipt", parent_request_id="req_tools", stream=False)
        body["input"].extend([call, {"type": "tool_result", "id": "result_receipt", "name": tool["name"],
                                    "call_id": call["call_id"], "content": [{"type": "text", "text": "found"}]}])
        result = client.post("/model/responses", headers={**headers, "Idempotency-Key": "req_receipt"}, json=body)
        assert result.status_code == 200 and result.json()["status"] == "completed", result.text
        assert calls[-1]["parent_request_id"] == calls[0]["request_id"]
        assert result.json()["output"][0]["id"] == "rs_fixture"
