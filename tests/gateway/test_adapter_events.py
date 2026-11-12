import asyncio
import json

import httpx
import pytest

from core.models import KemoRequest
from core.provider_contract import ProviderEventKind as Kind, RequestContext
from gateway.adapters.builtin.openai_chat import OpenAIChat
from gateway.domain import Upstream
from gateway.transport import Transport
from tests.support.gateway import add_upstream, instance, key, register


TOOL = {"name": "lookup", "description": "Look up a city", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
}}


def frames_for(adapter, arguments):
    chunks = [(value[:8], value[8:]) for value in arguments]
    if adapter == "openai-chat":
        frames = [{"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": index, "id": f"remote-{index}", "function": {"name": "lookup", "arguments": ""}}
            for index in range(2)
        ]}}]}]
        frames.append({"choices": [{"delta": {"content": "Working"}}]})
        for part in range(2):
            for index in range(2):
                frames.append({"choices": [{"delta": {"tool_calls": [{
                    "index": index, "function": {"arguments": chunks[index][part]},
                }]}}]})
        return [*frames, {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
    if adapter == "anthropic-messages":
        frames = [{"type": "message_start", "message": {"id": "remote", "type": "message", "usage": {}}}]
        frames.extend({"type": "content_block_start", "index": index, "content_block": {
            "type": "tool_use", "id": f"remote-{index}", "name": "lookup", "input": {},
        }} for index in range(2))
        frames.extend([
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "Working"}},
        ])
        for part in range(2):
            for index in range(2):
                frames.append({"type": "content_block_delta", "index": index, "delta": {
                    "type": "input_json_delta", "partial_json": chunks[index][part],
                }})
        return [*frames, *({"type": "content_block_stop", "index": index} for index in range(3)),
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"}]
    if adapter == "cohere-chat":
        frames = [{"type": "message-start", "id": "remote"}]
        frames.extend({"type": "tool-call-start", "index": index, "delta": {"message": {"tool_calls": {
            "id": f"remote-{index}", "function": {"name": "lookup", "arguments": ""},
        }}}} for index in range(2))
        frames.append({"type": "content-delta", "delta": {"message": {"content": {"text": "Working"}}}})
        for part in range(2):
            for index in range(2):
                frames.append({"type": "tool-call-delta", "index": index, "delta": {"message": {"tool_calls": {
                    "function": {"arguments": chunks[index][part]},
                }}}})
        return [*frames, *({"type": "tool-call-end", "index": index} for index in range(2)),
                {"type": "message-end", "delta": {"finish_reason": "TOOL_CALL"}}]
    calls = [{"type": "function_call", "id": f"native-{index}", "call_id": f"remote-{index}",
              "name": "lookup", "arguments": value} for index, value in enumerate(arguments)]
    message = {"type": "message", "id": "native-message", "phase": "commentary",
               "content": [{"type": "output_text", "text": "Working"}]}
    frames = [{"type": "response.output_item.added", "item": {**call, "arguments": ""}} for call in calls]
    frames.extend([
        {"type": "response.output_item.added", "item": message},
        {"type": "response.output_text.delta", "item_id": "native-message", "delta": "Working"},
    ])
    for part in range(2):
        for index in range(2):
            frames.append({"type": "response.function_call_arguments.delta", "item_id": f"native-{index}",
                           "delta": chunks[index][part]})
    return [*frames, {"type": "response.completed", "response": {
        "id": "remote", "status": "completed", "output": [*calls, message],
    }}]


def decode_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("adapter", ["openai-chat", "anthropic-messages", "cohere-chat", "openai-responses"])
@pytest.mark.parametrize("invalid", [None, "json", "schema"])
def test_interleaved_tool_deltas_identity_phase_and_execution_gate(tmp_path, monkeypatch, adapter, invalid):
    arguments = ['{"city":"Beijing"}', '{"city":"Paris"}']
    if invalid == "json":
        arguments[1] = '{"city":"Paris"'
    elif invalid == "schema":
        arguments[1] = '{"city":1}'
    frames = frames_for(adapter, arguments)
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    with instance(tmp_path, monkeypatch, lambda _: httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream"},
    )) as (client, _):
        add_upstream(client, adapter=adapter, auth_scheme="none")
        register(client, capabilities={"streaming": True, "tools": {"function_calling": True, "parallel_calls": True}})
        response = client.post("/model/responses", headers={
            "Authorization": "Bearer " + key(client)["token"], "X-Kemo-Protocol-Version": "2.0",
            "Idempotency-Key": "req_events",
        }, json={"protocol_version": "2.0", "request_id": "req_events", "model": "stable", "stream": True,
                 "tools": [TOOL], "parallel_tool_calls": True, "input": [{
                     "type": "message", "id": "msg_user", "role": "user",
                     "content": [{"type": "text", "text": "Look up two cities"}],
                 }]})
        assert response.status_code == 200, response.text
        events = decode_events(response)
    terminal = events[-1]["response"]
    deltas = [event for event in events if event["type"] == "tool_call.arguments.delta"]
    completed = [event for event in events if event["type"] == "tool_call.completed"]
    assert len(deltas) == 4, json.dumps(events, ensure_ascii=True)
    assert deltas[0]["call_id"] == deltas[2]["call_id"] != deltas[1]["call_id"] == deltas[3]["call_id"]
    for index, first in enumerate(deltas[:2]):
        matching = [event for event in deltas if event["call_id"] == first["call_id"]]
        assert "".join(event["delta"] for event in matching) == arguments[index]
        assert {event["item_id"] for event in matching} == {first["item_id"]}
        assert {event["name"] for event in matching} == {"lookup"}
    starts = [event["item"] for event in events if event["type"] == "output_item.added"]
    message_start = next(item for item in starts if item["type"] == "message")
    assert message_start["phase"] == "commentary"
    assert next(item for item in terminal["output"] if item["type"] == "message")["phase"] == "commentary"
    if invalid:
        assert not completed
        assert terminal["status"] == "incomplete"
        assert terminal["incomplete_details"]["reason"] == "invalid_tool_arguments"
        assert not any(item["type"] == "tool_call" for item in terminal["output"])
    else:
        assert terminal["status"] == "requires_action"
        assert len(completed) == 2
        for event in completed:
            delta = next(delta for delta in deltas if delta["call_id"] == event["call_id"])
            item = next(item for item in terminal["output"] if item.get("call_id") == event["call_id"])
            assert event["item_id"] == delta["item_id"] == item["id"]
            assert event["item"] == item


@pytest.mark.parametrize("adapter", ["openai-chat", "openai-responses", "anthropic-messages"])
def test_refusal_events_match_terminal_message(tmp_path, monkeypatch, adapter):
    refusal = "Cannot comply."
    if adapter == "openai-chat":
        frames = [
            {"choices": [{"delta": {"refusal": "Cannot "}}]},
            {"choices": [{"delta": {"refusal": "comply."}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
        ]
    elif adapter == "openai-responses":
        frames = [
            {"type": "response.refusal.delta", "item_id": "refusal", "delta": "Cannot "},
            {"type": "response.refusal.delta", "item_id": "refusal", "delta": "comply."},
            {"type": "response.refusal.done", "item_id": "refusal", "refusal": refusal},
            {"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "message", "id": "refusal", "content": [{"type": "refusal", "refusal": refusal}]},
            ]}},
        ]
    else:
        frames = [
            {"type": "message_start", "message": {"type": "message", "id": "remote", "usage": {}}},
            {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_details": {"explanation": refusal}}},
            {"type": "message_stop"},
        ]
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    with instance(tmp_path, monkeypatch, lambda _: httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream"},
    )) as (client, _):
        from tests.support.gateway import invoke
        add_upstream(client, adapter=adapter, auth_scheme="none")
        register(client)
        events = decode_events(invoke(client, key(client)["token"], stream=True))
    deltas = [event for event in events if event["type"] == "output_refusal.delta"]
    done = next(event for event in events if event["type"] == "output_refusal.done")
    terminal = events[-1]["response"]
    assert "".join(event["delta"] for event in deltas) == done["text"] == refusal
    assert {event["item_id"] for event in deltas} == {done["item_id"]}
    assert next(item for item in terminal["output"] if item["id"] == done["item_id"])["refusal"] == refusal
    assert done["sequence"] < events[-1]["sequence"]
    assert all(done["sequence"] < event["sequence"] for event in events if event["type"] == "usage.updated")


@pytest.mark.parametrize("field,parts", [
    ("content", ["π", "0 的中文版", "转换完成。"]),
    ("refusal", ["Cannot ", "comply ", "with this request."]),
])
def test_openai_chat_per_chunk_usage_does_not_close_content_early(tmp_path, monkeypatch, field, parts):
    frames = [{
        "id": "remote",
        "choices": [],
        "usage": {"prompt_tokens": 17, "completion_tokens": 0},
    }]
    frames.extend({
        "id": "remote",
        "choices": [{"index": 0, "delta": {field: part}, "finish_reason": None}],
        "usage": {"prompt_tokens": 17, "completion_tokens": index},
    } for index, part in enumerate(parts, start=1))
    frames.append({
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 5},
    })
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    with instance(tmp_path, monkeypatch, lambda _: httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream"},
    )) as (client, _):
        from tests.support.gateway import invoke
        add_upstream(client, auth_scheme="none")
        register(client)
        events = decode_events(invoke(client, key(client)["token"], stream=True))
    terminal = events[-1]["response"]
    assert terminal["status"] == "completed", terminal
    prefix = "output_text" if field == "content" else "output_refusal"
    deltas = [event for event in events if event["type"] == prefix + ".delta"]
    done = [event for event in events if event["type"] == prefix + ".done"]
    assert [event["delta"] for event in deltas] == parts
    assert len(done) == 1 and done[0]["text"] == "".join(parts)
    usage = [event for event in events if event["type"] == "usage.updated"]
    assert len(usage) == 1
    assert deltas[-1]["sequence"] < done[0]["sequence"] < usage[0]["sequence"]
    assert usage[0]["usage"]["output_tokens"] == terminal["usage"]["output_tokens"] == 5
    assert terminal["usage"]["input_tokens"] == 17
    message = next(item for item in terminal["output"] if item["type"] == "message")
    if field == "content":
        assert message["content"][0]["text"] == "".join(parts)
    else:
        assert message["refusal"] == "".join(parts)


def test_long_tool_arguments_are_visible_before_upstream_finishes():
    class Chunks(httpx.AsyncByteStream):
        finished = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"remote","function":{"name":"lookup","arguments":"{\\"city\\":\\""}}]}}]}\n\n'
            yield ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": "x" * 8192 + '"}'},
            }]}, "finish_reason": "tool_calls"}]}) + "\n\n").encode()
            self.finished = True

    async def exercise():
        chunks = Chunks()
        transport = Transport(http_transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=chunks)))
        upstream = Upstream(id="local", name="local", adapter_id="openai-chat",
                            base_url="https://example.com", auth_scheme="none")
        request = KemoRequest.create(model="stable", stream=True, tools=[TOOL], input=[{
            "type": "message", "id": "msg_user", "role": "user", "content": [{"type": "text", "text": "hello"}],
        }])
        stream = OpenAIChat(transport).stream(
            request, RequestContext("t", "s", "req_events", "resp_events", "trace_events"),
            "remote", upstream, {},
        )
        try:
            first = await anext(stream)
            assert first.kind == Kind.TOOL_ARGUMENTS_DELTA and first.delta == '{"city":"'
            assert not chunks.finished
            rest = [event async for event in stream]
            assert rest[-1].result.output[0]["arguments"]["city"] == "x" * 8192
            assert rest[-1].result.output[0]["id"] == first.item_id
        finally:
            await stream.aclose()
            await transport.close()

    asyncio.run(exercise())
