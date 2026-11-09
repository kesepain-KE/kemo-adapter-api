from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from core.event_assembler import EventAssembler
from core.models import KemoRequest, ModelCapabilities
from core.provider_contract import ProviderEventKind as Kind, ProviderException, RequestContext
from gateway.adapters.builtin.anthropic import Anthropic
from gateway.adapters.builtin.cloudflare_workers_ai import CloudflareWorkersAI
from gateway.adapters.builtin.cohere_chat import CohereChat
from gateway.adapters.builtin.gemini import Gemini
from gateway.adapters.builtin.openai_chat import OpenAIChat
from gateway.adapters.builtin.openai_responses import OpenAIResponses
from gateway.adapters.builtin.perplexity_agent import PerplexityAgent
from gateway.runtime import BUILTINS, RuntimeRegistry, RuntimeUpstream, freeze
from gateway.transport import Transport, upstream_error


CONTEXT = RequestContext("tenant", "subject", "req_test", "resp_test", "trace_test", "gateway system")
UPSTREAM = RuntimeUpstream(
    "offline", "openai-chat", "https://provider.invalid", 10, True, False,
    "none", freeze({}), freeze({}), (), "{}",
)
TOOL = {"name": "lookup", "description": "Look up a city", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"],
}}


def chat_request(**overrides):
    return KemoRequest.model_validate({
        "protocol_version": "2.0", "request_id": "req_test", "model": "public", "stream": False,
        "input": [{"type": "message", "id": "msg_user", "role": "user",
                   "content": [{"type": "text", "text": "hello"}]}],
        **overrides,
    })


def chat_response(**extra):
    return {
        "id": "remote", "choices": [{
            "index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 10, "completion_tokens": 5}, **extra,
    }


def cohere_response(**extra):
    return {
        "id": "cohere-response", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "hello"},
        ]}, "finish_reason": "COMPLETE", "usage": {
            "billed_units": {"input_tokens": 17, "output_tokens": 12},
            "tokens": {"input_tokens": 215, "output_tokens": 24},
        }, **extra,
    }


def sse(*frames):
    return httpx.Response(
        200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
        headers={"content-type": "text/event-stream"},
    )


def invoke(adapter_type, handler, request=None, *, upstream=UPSTREAM, stream=False):
    async def run():
        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            adapter = adapter_type(transport)
            arguments = (request or chat_request(), CONTEXT, "remote-model", upstream, {})
            if stream:
                return [event async for event in adapter.stream(*arguments)]
            return await adapter.execute(*arguments)
        finally:
            await transport.close()
    return asyncio.run(run())


@pytest.mark.parametrize("adapter_type", [OpenAIChat, OpenAIResponses, Anthropic, Gemini, CohereChat, PerplexityAgent])
@pytest.mark.parametrize("mixed_content", [False, True])
def test_json_tool_receipt_reaches_upstream(adapter_type, mixed_content):
    receipt = {
        "execution_id": "exec_saved",
        "execution": {"status": "completed", "execution_live": False},
        "original_call": {"name": "lookup", "arguments": {"city": "Beijing"}},
        "result": {"ok": True, "result": {"weather": "晴", "temperature": 0, "warnings": []}},
    }
    content = [{"type": "json", "data": receipt}]
    expected = json.dumps(receipt, ensure_ascii=False)
    if mixed_content:
        content = [{"type": "text", "text": "before"}, *content, {"type": "text", "text": "after"}]
        expected = "before\n" + expected + "\nafter"
    request = chat_request(tools=[TOOL], input=[
        {"type": "tool_call", "id": "call_prior", "call_id": "callid_prior",
         "name": "lookup", "arguments": {"city": "Beijing"}},
        {"type": "tool_result", "id": "result_prior", "call_id": "callid_prior",
         "name": "lookup", "content": content},
    ])

    def handler(http_request):
        body = json.loads(http_request.content)
        if adapter_type is OpenAIResponses:
            result = next(item for item in body["input"] if item.get("type") == "function_call_output")
            assert result["call_id"] == "callid_prior"
            actual = result["output"]
            raw = {"id": "remote", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "received"}]},
            ]}
        elif adapter_type is Anthropic:
            result = body["messages"][-1]["content"][0]
            assert result["tool_use_id"] == "callid_prior"
            actual = result["content"]
            raw = {"id": "remote", "type": "message", "stop_reason": "end_turn",
                   "content": [{"type": "text", "text": "received"}]}
        elif adapter_type is Gemini:
            result = body["contents"][-1]["parts"][0]["functionResponse"]
            assert result["name"] == "lookup"
            actual = result["response"]["result"]
            raw = {"candidates": [{"content": {"parts": [{"text": "received"}]}, "finishReason": "STOP"}]}
        else:
            result = body["messages"][-1]
            assert result["tool_call_id"] == "callid_prior"
            actual = result["content"]
            raw = cohere_response() if adapter_type is CohereChat else chat_response()
        assert actual == expected
        return httpx.Response(200, json=raw)

    assert invoke(adapter_type, handler, request).status == "completed"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reasoning", [
    None, {"enabled": False}, {"enabled": True, "effort": "max"},
])
def test_deepseek_reasoning_toggle_and_effort_are_encoded_for_both_transports(stream, reasoning):
    upstream = replace(UPSTREAM, profile=freeze({
        "thinking_mode": True, "reasoning_effort_map": {"low": "low", "high": "high", "max": "max"},
    }))
    def handler(request):
        body = json.loads(request.content)
        if reasoning is None:
            assert "thinking" not in body and "reasoning_effort" not in body
        else:
            assert body["thinking"] == {"type": "enabled" if reasoning["enabled"] else "disabled"}
            if reasoning.get("effort") == "max":
                assert body["reasoning_effort"] == "max"
            else:
                assert "reasoning_effort" not in body
        if stream:
            return sse({"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json=chat_response())
    result = invoke(OpenAIChat, handler, chat_request(reasoning=reasoning), upstream=upstream, stream=stream)
    assert (result[-1].result if stream else result).status == "completed"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("phase", [None, "commentary", "final_answer"])
def test_responses_reasoning_request_and_summary_round_trip(stream, phase):
    raw = {
        "id": "remote", "status": "completed", "output": [
            {"type": "reasoning", "id": "rs_remote", "summary": [{"type": "summary_text", "text": "consider"}]},
            {"type": "message", "id": "msg_remote", "content": [{"type": "output_text", "text": "hello"}]},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 2}},
    }
    if phase is not None:
        raw["output"][1]["phase"] = phase
    def handler(request):
        assert json.loads(request.content)["reasoning"] == {"effort": "high", "summary": "auto"}
        return sse({"type": "response.completed", "response": raw}) if stream else httpx.Response(200, json=raw)
    result = invoke(OpenAIResponses, handler, chat_request(
        reasoning={"enabled": True, "effort": "high", "return": "auto"},
    ), stream=stream)
    result = result[-1].result if stream else result
    assert result.output[0]["summary"] == "consider" and result.usage.reasoning_tokens == 2
    assert result.output[1]["phase"] == (phase or "final_answer")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("kind", "status", "phase"), [
    ("answer", "completed", "final_answer"),
    ("refusal", "completed", "final_answer"),
    ("tool", "requires_action", "commentary"),
    ("truncated", "incomplete", "final_answer"),
])
def test_chat_output_phase_matches_completion(stream, kind, status, phase):
    content = {"content": "hello"}
    reason = "stop"
    if kind == "refusal":
        content = {"refusal": "cannot answer"}
    elif kind == "tool":
        content["tool_calls"] = [{
            "id": "remote-call", "type": "function",
            "function": {"name": "lookup", "arguments": '{"city":"Beijing"}'},
        }]
        reason = "tool_calls"
    elif kind == "truncated":
        reason = "length"

    def handler(request):
        if not stream:
            return httpx.Response(200, json={
                "id": "remote", "choices": [{"index": 0, "message": content, "finish_reason": reason}],
            })
        delta = dict(content)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
        return sse(
            {"id": "remote", "choices": [{"index": 0, "delta": delta}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        )

    result = invoke(OpenAIChat, handler, chat_request(tools=[TOOL]), stream=stream)
    result = result[-1].result if stream else result
    assert result.status == status
    assert result.output[0]["phase"] == phase
    if kind == "truncated":
        assert result.incomplete_details == {"reason": "output_truncated"}


@pytest.mark.parametrize("adapter_type", [Anthropic, Gemini])
@pytest.mark.parametrize("tool_call", [False, True])
def test_native_message_phase_distinguishes_answer_from_tool_preamble(adapter_type, tool_call):
    if adapter_type is Anthropic:
        raw = {"id": "remote", "type": "message", "content": [{"type": "text", "text": "hello"}],
               "stop_reason": "tool_use" if tool_call else "end_turn"}
        if tool_call:
            raw["content"].append({"type": "tool_use", "id": "remote-call",
                                   "name": "lookup", "input": {"city": "Beijing"}})
    else:
        parts = [{"text": "hello"}]
        if tool_call:
            parts.append({"functionCall": {"name": "lookup", "args": {"city": "Beijing"}}})
        raw = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    result = invoke(adapter_type, lambda _: httpx.Response(200, json=raw), chat_request(tools=[TOOL]))
    assert result.status == ("requires_action" if tool_call else "completed")
    assert result.output[0]["phase"] == ("commentary" if tool_call else "final_answer")


def test_responses_without_native_phase_marks_tool_preamble_as_commentary():
    raw = {"id": "remote", "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "Checking"}]},
        {"type": "function_call", "name": "lookup", "arguments": '{"city":"Beijing"}', "call_id": "remote-call"},
    ]}
    result = invoke(OpenAIResponses, lambda _: httpx.Response(200, json=raw), chat_request(tools=[TOOL]))
    assert result.status == "requires_action"
    assert result.output[0]["phase"] == "commentary"


def test_cohere_chat_encode():
    request = chat_request(
        system_prompt="request system", tools=[TOOL], tool_choice={"mode": "required"},
        parallel_tool_calls=False,
        generation={"max_output_tokens": 100, "temperature": 0, "top_p": 0.8,
                    "top_k": 4, "stop": ["END"], "seed": 0},
        input=[
            {"type": "message", "id": "msg_user", "role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "source": {"kind": "url", "uri": "https://example.com/image.png"}},
            ]},
            {"type": "tool_call", "id": "call_prior", "call_id": "callid_prior",
             "name": "lookup", "arguments": {"city": "Beijing"}},
            {"type": "tool_result", "id": "result_prior", "call_id": "callid_prior",
             "name": "lookup", "content": [{"type": "text", "text": "sunny"}]},
        ],
    )
    def handler(http_request):
        assert http_request.url.path == "/v2/chat"
        body = json.loads(http_request.content)
        assert body["messages"][0] == {"role": "system", "content": "gateway system\n\nrequest system"}
        assert body["messages"][1]["content"][1] == {
            "type": "image_url", "image_url": {"url": "https://example.com/image.png"},
        }
        call = body["messages"][2]["tool_calls"][0]
        assert call["id"] == "callid_prior" and json.loads(call["function"]["arguments"]) == {"city": "Beijing"}
        assert body["messages"][3] == {"role": "tool", "tool_call_id": "callid_prior", "content": "sunny"}
        assert body["tools"] == [{"type": "function", "function": TOOL}]
        assert body["tool_choice"] == "REQUIRED"
        assert {field: body[field] for field in ("max_tokens", "temperature", "p", "k", "stop_sequences", "seed")} == {
            "max_tokens": 100, "temperature": 0, "p": 0.8, "k": 4, "stop_sequences": ["END"], "seed": 0,
        }
        assert "parallel_tool_calls" not in body and "top_p" not in body
        return httpx.Response(200, json=cohere_response())
    assert invoke(CohereChat, handler, request).status == "completed"


@pytest.mark.parametrize(("choice", "expected_names", "expected_choice"), [
    ({"mode": "auto"}, ["lookup", "other"], None),
    ({"mode": "none"}, ["lookup", "other"], "NONE"),
    ({"mode": "named", "name": "lookup"}, ["lookup"], "REQUIRED"),
    ({"mode": "allowed", "allowed_tools": ["lookup"], "allowed_mode": "required"}, ["lookup"], "REQUIRED"),
])
def test_cohere_tool_choice(choice, expected_names, expected_choice):
    request = chat_request(tools=[TOOL, {**TOOL, "name": "other"}], tool_choice=choice)
    body = CohereChat(None).encode(request, CONTEXT, "command")
    assert [tool["function"]["name"] for tool in body["tools"]] == expected_names
    assert body.get("tool_choice") == expected_choice
    assert body["messages"][-1]["content"] == "hello"


def test_cohere_chat_result():
    raw = cohere_response(message={"role": "assistant", "content": [
        {"type": "thinking", "thinking": "consider"}, {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ], "tool_calls": [{"id": "remote-call", "type": "function", "function": {
        "name": "lookup", "arguments": '{"city":"Beijing"}',
    }}]}, finish_reason="TOOL_CALL")
    result = invoke(CohereChat, lambda _: httpx.Response(200, json=raw))
    assert result.status == "requires_action"
    assert result.provider_response_id == "cohere-response"
    assert result.output[0]["content"] == "consider"
    assert result.output[1]["content"][0]["text"] == "firstsecond"
    assert result.output[1]["phase"] == "commentary"
    assert result.output[2]["arguments"] == {"city": "Beijing"}
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens) == (17, 12, 29)
    assert result.usage.measurement.exact


@pytest.mark.parametrize(("reason", "status", "incomplete"), [
    ("COMPLETE", "completed", None), ("STOP_SEQUENCE", "completed", None),
    ("MAX_TOKENS", "incomplete", "output_truncated"),
    ("ERROR", "incomplete", "content_filtered"), ("ERROR_TOXIC", "incomplete", "content_filtered"),
    ("ERROR_LIMIT", "incomplete", "upstream_stopped"), ("USER_CANCEL", "incomplete", "upstream_stopped"),
    ("TIMEOUT", "incomplete", "upstream_stopped"),
])
def test_cohere_finish_reason(reason, status, incomplete):
    result = CohereChat.result(cohere_response(finish_reason=reason))
    assert result.status == status
    assert result.incomplete_details == ({"reason": incomplete} if incomplete else None)


def test_cohere_missing_usage_is_unknown():
    result = invoke(CohereChat, lambda _: httpx.Response(200, json=cohere_response(usage=None)))
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    assert result.usage.measurement.mode == "unknown"
    assert CohereChat.usage({"billed_units": {"input_tokens": 17.0, "output_tokens": 12.0}}).total_tokens == 29


def test_cohere_chat_stream():
    frames = [
        {"type": "message-start", "id": "cohere-stream", "delta": {"message": {"role": "assistant"}}},
        {"type": "content-start", "index": 0},
        {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"thinking": "think"}}}},
        {"type": "content-delta", "index": 1, "delta": {"message": {"content": {"text": "hello"}}}},
        {"type": "content-end", "index": 1},
        {"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {
            "id": "remote-call", "type": "function", "function": {"name": "lookup", "arguments": "{"},
        }}}},
        {"type": "tool-call-delta", "index": 0, "delta": {"message": {"tool_calls": {
            "function": {"arguments": '"city":"Beijing"}'},
        }}}},
        {"type": "tool-call-end", "index": 0},
        {"type": "message-end", "delta": {
            "finish_reason": "TOOL_CALL", "usage": cohere_response()["usage"],
        }},
    ]
    def handler(http_request):
        assert json.loads(http_request.content)["stream"] is True
        return sse(*frames)
    events = invoke(CohereChat, handler, stream=True)
    assert [event.kind for event in events] == [
        Kind.REASONING_CONTENT_DELTA, Kind.TEXT_DELTA, Kind.TOOL_ARGUMENTS_DELTA,
        Kind.TOOL_ARGUMENTS_DELTA, Kind.TOOL_COMPLETED, Kind.USAGE, Kind.COMPLETED,
    ]
    assembled = [
        EventAssembler.assemble(event, request_id=CONTEXT.request_id, response_id=CONTEXT.response_id, sequence=index)
        for index, event in enumerate(events[:-1], start=1)
    ]
    assert [event.type for event in assembled] == [
        "reasoning.content.delta", "output_text.delta", "tool_call.arguments.delta",
        "tool_call.arguments.delta", "tool_call.completed", "usage.updated",
    ]
    assert assembled[0].delta == "think" and assembled[0].item is None
    assert assembled[1].delta == "hello"
    result = events[-1].result
    assert result.status == "requires_action" and result.provider_response_id == "cohere-stream"
    assert result.output[0]["id"] == events[0].item_id
    assert result.output[1]["id"] == events[1].item_id
    assert result.output[1]["phase"] == "commentary"
    assert result.output[2] == events[4].item
    assert "".join(event.delta for event in events if event.kind == Kind.TOOL_ARGUMENTS_DELTA) == '{"city":"Beijing"}'
    assert result.output[2]["arguments"] == {"city": "Beijing"} and result.usage.total_tokens == 29


def test_openai_chat_stream_events_follow_protocol_contract():
    events = invoke(OpenAIChat, lambda _: sse(
        {"id": "openai-stream", "choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]},
        {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "remote-call", "function": {"name": "lookup", "arguments": '{"city":"Beijing"}'},
        }]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 12}},
    ), chat_request(tools=[TOOL]), stream=True)
    assembled = [
        EventAssembler.assemble(event, request_id=CONTEXT.request_id, response_id=CONTEXT.response_id, sequence=index)
        for index, event in enumerate(events[:-1], start=1)
    ]
    assert [event.type for event in assembled] == [
        "reasoning.content.delta", "output_text.delta", "tool_call.arguments.delta",
        "usage.updated", "tool_call.completed",
    ]
    assert assembled[0].delta == "think" and assembled[0].item is None
    assert assembled[1].delta == "hello"
    result = events[-1].result
    assert result.status == "requires_action" and result.provider_response_id == "openai-stream"
    assert result.output[1]["phase"] == "commentary"
    assert result.output[0]["id"] == assembled[0].item_id
    assert result.output[1]["id"] == assembled[1].item_id
    assert result.output[2]["id"] == assembled[-1].item.id
    assert result.output[2]["call_id"] == assembled[-1].call_id == assembled[-1].item.call_id
    assert result.output[2]["name"] == assembled[-1].item.name == "lookup"
    assert result.output[2]["arguments"] == assembled[-1].item.arguments == {"city": "Beijing"}
    assert result.usage.total_tokens == 29


@pytest.mark.parametrize("frames", [
    [],
    [{"type": "message-start", "id": "remote"}],
    [{"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {
        "id": "call", "function": {"name": "lookup", "arguments": ""},
    }}}}, {"type": "message-end", "delta": {"finish_reason": "TOOL_CALL"}}],
])
def test_cohere_stream_requires_complete_terminal(frames):
    with pytest.raises(ProviderException) as caught:
        invoke(CohereChat, lambda _: sse(*frames), stream=True)
    assert caught.value.error.details["reason"] == "UPSTREAM_TRUNCATED_STREAM"


def test_cohere_discovery_uses_names_and_pagination():
    paths = []
    def handler(request):
        paths.append(request.url)
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert request.url.params["endpoint"] == "chat"
        if len(paths) == 1:
            return httpx.Response(200, json={"models": [{"name": "command-a"}], "next_page_token": "next+/="})
        assert request.url.params["page_token"] == "next+/="
        return httpx.Response(200, json={"models": [{"name": "command-b"}, {"name": "command-a"}]})
    async def run():
        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            return await CohereChat(transport).discover(UPSTREAM, {})
        finally:
            await transport.close()
    assert asyncio.run(run()) == ["command-a", "command-b"] and len(paths) == 2


def test_perplexity_encode():
    request = chat_request(generation={
        "max_output_tokens": 42, "presence_penalty": 1, "frequency_penalty": 1,
        "logit_bias": {"1": 2}, "seed": 7,
    })
    body = PerplexityAgent(None).encode(request, CONTEXT, "sonar", UPSTREAM, True)
    assert body["max_tokens"] == 42 and body["stream"] is True
    assert not {"presence_penalty", "frequency_penalty", "logit_bias", "seed", "stream_options",
                "return_citations", "search_recency_filter", "search_domain_filter"} & body.keys()
    raw_usage = {"prompt_tokens": 100, "completion_tokens": 50, "citation_tokens": 20}
    assert PerplexityAgent.usage(raw_usage).input_tokens == 120
    assert raw_usage["prompt_tokens"] == 100


def test_perplexity_result():
    def handler(request):
        assert request.url.path == "/chat/completions"
        return httpx.Response(200, json=chat_response(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "citation_tokens": 20},
            citations=["https://example.com/source"],
        ))
    result = invoke(PerplexityAgent, handler)
    assert result.output[0]["content"][0]["text"] == "hello"
    assert (result.usage.input_tokens, result.usage.total_tokens) == (120, 170)
    assert result.metadata == {} and result.extensions == {}


def test_perplexity_stream():
    events = invoke(PerplexityAgent, lambda _: sse(
        {"id": "sonar-stream", "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "citation_tokens": 20},
         "citations": ["https://example.com"]},
    ), stream=True)
    assert [event.kind for event in events] == [Kind.TEXT_DELTA, Kind.USAGE, Kind.COMPLETED]
    assert events[-1].result.usage.total_tokens == 170


def test_cloudflare_result():
    def handler(request):
        assert request.url.raw_path == b"/run/remote-model"
        body = json.loads(request.content)
        assert body == {
            "messages": [{"role": "system", "content": "gateway system"}, {"role": "user", "content": "hello"}],
            "stream": False, "max_tokens": 64, "temperature": 0,
        }
        return httpx.Response(200, json={
            "success": True, "result": {"response": "cloudflare", "usage": {
                "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
            }}, "errors": [], "messages": [],
        })
    result = invoke(CloudflareWorkersAI, handler, chat_request(generation={"max_output_tokens": 64, "temperature": 0}))
    assert result.status == "completed" and result.output[0]["content"][0]["text"] == "cloudflare"
    assert result.output[0]["phase"] == "final_answer"
    assert result.usage.total_tokens == 30 and result.usage.measurement.exact


def test_cloudflare_encoded_model_path_and_missing_usage():
    async def run():
        def handler(request):
            assert request.url.raw_path == b"/accounts/offline/ai/run/%40cf%2Fmeta%2Fllama-3.1-8b-instruct"
            return httpx.Response(200, json={"success": True, "result": {"response": "hello"}})
        transport = Transport(http_transport=httpx.MockTransport(handler))
        try:
            return await CloudflareWorkersAI(transport).execute(
                chat_request(), CONTEXT, "@cf/meta/llama-3.1-8b-instruct",
                replace(UPSTREAM, base_url="https://provider.invalid/accounts/offline/ai"), {},
            )
        finally:
            await transport.close()
    result = asyncio.run(run())
    assert result.usage.measurement.mode == "unknown" and result.usage.total_tokens is None


@pytest.mark.parametrize("raw", [
    {"success": False, "errors": [{"code": 1000, "message": "secret-credential"}]},
    {"success": True, "result": {}},
])
def test_cloudflare_errors_are_normalized(raw):
    with pytest.raises(ProviderException) as caught:
        invoke(CloudflareWorkersAI, lambda _: httpx.Response(200, json=raw))
    assert caught.value.error.code == "PROVIDER_BAD_RESPONSE"
    assert "secret-credential" not in caught.value.error.model_dump_json()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("current", ["max_tokens", "max_completion_tokens"])
def test_max_tokens_retry_once(stream, current, caplog):
    bodies = []
    other = "max_completion_tokens" if current == "max_tokens" else "max_tokens"
    upstream = replace(UPSTREAM, profile=freeze({"max_tokens_field": current}))
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": f"Unrecognized {current}; sensitive body"}})
        if stream:
            return sse({"id": "remote", "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json=chat_response())
    result = invoke(OpenAIChat, handler, chat_request(generation={"max_output_tokens": 64}),
                    upstream=upstream, stream=stream)
    assert (result[-1].result if stream else result).status == "completed"
    assert len(bodies) == 2
    assert bodies[0][current] == 64 and other not in bodies[0]
    assert bodies[1][other] == 64 and current not in bodies[1]
    assert upstream.profile["max_tokens_field"] == current
    assert "upstream=offline" in caplog.text and "sensitive body" not in caplog.text


@pytest.mark.parametrize("stream", [False, True])
def test_max_tokens_no_retry_without_hint(stream):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, text="model not available")
    with pytest.raises(ProviderException):
        invoke(OpenAIChat, handler, chat_request(generation={"max_output_tokens": 64}), stream=stream)
    assert len(calls) == 1


@pytest.mark.parametrize(("status", "generation"), [(401, {"max_output_tokens": 64}), (400, {})])
@pytest.mark.parametrize("stream", [False, True])
def test_max_tokens_requires_400_and_token_limit(status, generation, stream):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="unsupported_parameter: max_tokens")
    with pytest.raises(ProviderException):
        invoke(OpenAIChat, handler, chat_request(generation=generation), stream=stream)
    assert len(calls) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_max_tokens_retry_only_once(stream):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, text="unknown_parameter")
    with pytest.raises(ProviderException) as caught:
        invoke(OpenAIChat, handler, chat_request(generation={"max_output_tokens": 64}), stream=stream)
    assert caught.value.error.provider_status == 400 and len(calls) == 2


@pytest.mark.parametrize("retry", [False, True])
def test_openai_empty_stream_is_truncated(retry):
    calls = []
    def handler(request):
        calls.append(request)
        if retry and len(calls) == 1:
            return httpx.Response(400, text="max_tokens is unsupported")
        return sse()
    with pytest.raises(ProviderException) as caught:
        invoke(OpenAIChat, handler, chat_request(generation={"max_output_tokens": 64}), stream=True)
    assert caught.value.error.details["reason"] == "UPSTREAM_TRUNCATED_STREAM"
    assert len(calls) == (2 if retry else 1)


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed = True


def test_openai_stream_failure_after_first_frame_does_not_retry():
    calls = []
    body = TrackedStream(
        [b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'],
        upstream_error(400),
    )
    body.error.error.details["upstream_body"] = "unsupported max_tokens"
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=body)
    with pytest.raises(ProviderException):
        invoke(OpenAIChat, handler, chat_request(generation={"max_output_tokens": 64}), stream=True)
    assert len(calls) == 1 and body.closed


def test_openai_stream_closes_on_consumer_cancellation():
    body = TrackedStream([b'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'])
    async def run():
        transport = Transport(http_transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)))
        try:
            events = OpenAIChat(transport).stream(chat_request(), CONTEXT, "remote", UPSTREAM, {})
            assert (await anext(events)).kind == Kind.TEXT_DELTA
            await events.aclose()
            assert body.closed
        finally:
            await transport.close()
    asyncio.run(run())


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (400, "PROVIDER_BAD_RESPONSE", False), (401, "PROVIDER_BAD_RESPONSE", False),
    (429, "RATE_LIMITED", True), (503, "PROVIDER_UNAVAILABLE", True),
])
def test_transport_error_body_captured(status, code, retryable):
    body = TrackedStream([b"prefix " + b"x" * 4088 + "中".encode(), b"not read"])
    with pytest.raises(ProviderException) as caught:
        invoke(OpenAIChat, lambda _: httpx.Response(status, stream=body))
    error = caught.value.error
    expected = (b"prefix " + b"x" * 4088 + "中".encode())[:4096].decode("utf-8", errors="replace")
    assert error.details["upstream_body"] == expected and error.details["reason"] == "UPSTREAM_ERROR"
    assert error.provider_status == status and error.code == code and error.retryable == retryable
    assert body.reads == 1 and body.closed


def test_transport_body_read_failure_preserves_http_error():
    body = TrackedStream([b"partial"], httpx.ReadError("failed body"))
    with pytest.raises(ProviderException) as caught:
        invoke(OpenAIChat, lambda _: httpx.Response(429, stream=body))
    assert caught.value.error.provider_status == 429 and caught.value.error.code == "RATE_LIMITED"
    assert caught.value.error.details["upstream_body"] == "partial" and body.closed


@pytest.mark.parametrize("adapter_type", [OpenAIChat, PerplexityAgent, CohereChat, CloudflareWorkersAI])
def test_runtime_multiple_choices_only_allowed_for_openai_chat(adapter_type):
    caps = ModelCapabilities(
        protocol_version="2.0", model="public", provider_id="gateway", provider_model="remote",
        supports_multiple_choices=True, limits={"max_choices": 2},
    )
    if adapter_type is OpenAIChat:
        RuntimeRegistry.validate_declaration(adapter_type(None), caps, UPSTREAM)
    else:
        with pytest.raises(ValueError, match="多候选"):
            RuntimeRegistry.validate_declaration(adapter_type(None), caps, UPSTREAM)


def test_runtime_registers_new_builtins():
    assert {adapter.manifest.id for adapter in BUILTINS} == {
        "openai-chat", "openai-responses", "anthropic-messages", "gemini",
        "openai-embedding", "cohere-rerank", "cohere-chat", "perplexity-agent", "cloudflare-workers-ai",
        "openai-images", "openai-transcription", "openai-speech", "openai-audio-chat",
        "openai-video", "openai-files", "gemini-speech", "gemini-video",
    }
