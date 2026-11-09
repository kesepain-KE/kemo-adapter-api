from __future__ import annotations

from contextlib import aclosing
from urllib.parse import urlencode

from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind, ProviderResult
from gateway.adapters.builtin.common import (
    DiscoverableAdapter, identifier, message, openai_messages, selected_tools, tool_item, tool_call_id,
)
from gateway.adapters.contract import AdapterManifest
from gateway.auth import BEARER, NO_AUTH
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class CohereChat(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="cohere-chat", name="Cohere Chat v2", operations=("llm",),
        streaming=True, auth_schemes=(BEARER, NO_AUTH),
        endpoints={"llm": "/v2/chat", "discovery": "/v1/models"},
        body_fields=("model", "messages", "stream", "max_tokens", "temperature", "p", "k",
                     "stop_sequences", "seed", "tools", "tool_choice"),
        usage_semantics="usage.billed_units; input excludes cache",
    )
    discovery_path = "/v1/models"

    def encode(self, request, context, model, stream=False):
        messages = openai_messages(request, context)
        for item in messages:
            content = item.get("content")
            if isinstance(content, list) and all(block["type"] == "text" for block in content):
                item["content"] = "\n".join(block["text"] for block in content)
        body = {"model": model, "messages": messages, "stream": stream}
        for source, target in (
            ("max_output_tokens", "max_tokens"), ("temperature", "temperature"),
            ("top_p", "p"), ("top_k", "k"), ("stop", "stop_sequences"), ("seed", "seed"),
        ):
            value = getattr(request.generation, source)
            if value is not None:
                body[target] = value
        if request.tools:
            tools = selected_tools(request)
            mode = request.tool_choice.mode
            if mode == "named":
                # Cohere accepts REQUIRED/NONE, not an OpenAI named-tool object.
                tools = [tool for tool in tools if tool.name == request.tool_choice.name]
            body["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": tool.parameters,
            }} for tool in tools]
            if mode == "none":
                body["tool_choice"] = "NONE"
            elif mode in {"required", "named"} or (
                mode == "allowed" and request.tool_choice.allowed_mode == "required"
            ):
                body["tool_choice"] = "REQUIRED"
        return body

    @staticmethod
    def usage(raw):
        raw = raw or {}
        # TODO(verify): confirm the HTTP/SDK usage envelope with real responses.
        source = (raw.get("billed_units") or (raw.get("meta") or {}).get("billed_units")
                  or (raw.get("usage") or {}).get("billed_units") or {})
        billed = dict(source)
        # The official schema uses numbers; integral floats are exact token counts.
        for field in ("input_tokens", "output_tokens"):
            value = billed.get(field)
            if isinstance(value, float) and value.is_integer():
                billed[field] = int(value)
        return normalize_usage(
            billed, input_field="input_tokens", output_field="output_tokens", input_includes_cache=False,
        )

    @classmethod
    def result(cls, raw, *, msg_id=None, reasoning_id=None, calls=None):
        content = raw["message"]
        blocks = content.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block["type"] == "text")
        thinking = "".join(block.get("thinking", "") for block in blocks if block["type"] == "thinking")
        output = []
        if thinking:
            output.append({"type": "reasoning", "id": reasoning_id or identifier("rs"), "content": thinking})
        if calls is None:
            calls = [tool_item(call["function"]["name"], call["function"].get("arguments"), call["id"])
                     for call in content.get("tool_calls") or []]
        if text:
            output.append(message(
                text, item_id=msg_id,
                phase="commentary" if calls or raw.get("finish_reason") == "TOOL_CALL" else "final_answer",
            ))
        output.extend(calls)
        reason = raw.get("finish_reason")
        status = "requires_action" if calls or reason == "TOOL_CALL" else "completed"
        incomplete = None
        if reason in {"MAX_TOKENS", "ERROR", "ERROR_TOXIC", "ERROR_LIMIT", "USER_CANCEL", "TIMEOUT"}:
            status = "incomplete"
            incomplete = {"reason": (
                "output_truncated" if reason == "MAX_TOKENS" else
                "content_filtered" if reason in {"ERROR", "ERROR_TOXIC"} else "upstream_stopped"
            )}
        if not output and status == "completed":
            status, incomplete = "incomplete", {"reason": "empty_output"}
        return ProviderResult(
            status, output, cls.usage(raw), raw.get("id"), incomplete_details=incomplete,
        )

    async def execute(self, request, context, model, upstream, headers):
        raw = await self.transport.json(upstream, "/v2/chat", headers, body=self.encode(request, context, model))
        if not isinstance(raw.get("message"), dict) or not raw.get("finish_reason"):
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE")
        return self.result(raw)

    async def stream(self, request, context, model, upstream, headers):
        msg_id, reasoning_id = identifier("msg"), identifier("rs")
        provider_id, text, thinking = None, "", ""
        calls, items = {}, {}
        async with aclosing(self.transport.events(
            upstream, "/v2/chat", headers, self.encode(request, context, model, True),
        )) as events:
            async for event in events:
                kind = event.get("type")
                delta = event.get("delta") or {}
                if kind == "message-start":
                    provider_id = event.get("id")
                elif kind == "content-delta":
                    content = delta["message"]["content"]
                    if content.get("text"):
                        text += content["text"]
                        yield Event(Kind.TEXT_DELTA, item_id=msg_id, content_index=0, delta=content["text"],
                                    provider_response_id=provider_id, item={"phase": "commentary"} if calls else None)
                    if content.get("thinking"):
                        thinking += content["thinking"]
                        yield Event(Kind.REASONING_CONTENT_DELTA, item_id=reasoning_id, delta=content["thinking"])
                elif kind == "tool-call-start":
                    call = delta["message"]["tool_calls"]
                    calls[event["index"]] = {
                        "id": call["id"], "name": call["function"]["name"],
                        "arguments": call["function"].get("arguments") or "",
                        "item_id": identifier("call"),
                    }
                    target = calls[event["index"]]
                    if target["arguments"]:
                        yield Event(Kind.TOOL_ARGUMENTS_DELTA, item_id=target["item_id"],
                                    call_id=tool_call_id(target["id"]), name=target["name"], delta=target["arguments"])
                elif kind == "tool-call-delta":
                    call = calls[event["index"]]
                    arguments = delta["message"]["tool_calls"]["function"].get("arguments") or ""
                    call["arguments"] += arguments
                    if arguments:
                        yield Event(Kind.TOOL_ARGUMENTS_DELTA, item_id=call["item_id"],
                                    call_id=tool_call_id(call["id"]), name=call["name"], delta=arguments)
                elif kind == "tool-call-end":
                    index = event["index"]
                    call = calls[index]
                    items[index] = tool_item(call["name"], call["arguments"], call["id"], item_id=call["item_id"])
                    item = items[index]
                    yield Event(Kind.TOOL_COMPLETED, item_id=item["id"], item=item,
                                call_id=item["call_id"], name=item["name"])
                elif kind == "message-end":
                    if not delta.get("finish_reason") or len(items) != len(calls):
                        raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")
                    result = self.result({
                        "id": provider_id, "message": {"content": [
                            {"type": "text", "text": text}, {"type": "thinking", "thinking": thinking},
                        ]}, "finish_reason": delta["finish_reason"], "usage": delta.get("usage"),
                        "meta": delta.get("meta") or event.get("meta"),
                    }, msg_id=msg_id, reasoning_id=reasoning_id, calls=[items[i] for i in sorted(items)])
                    yield Event(Kind.USAGE, usage=result.usage)
                    yield Event(Kind.INCOMPLETE if result.status == "incomplete" else Kind.COMPLETED, result=result)
                    return
                elif kind == "error":
                    raise upstream_error(code="UPSTREAM_STREAM_ERROR")
        raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")

    async def discover(self, upstream, headers):
        models, page_token = set(), None
        while True:
            params = {"endpoint": "chat", "page_size": 1000}
            if page_token:
                params["page_token"] = page_token
            raw = await self.transport.json(
                upstream, self.discovery_path + "?" + urlencode(params), headers, method="GET",
            )
            if not isinstance(raw.get("models"), list):
                raise ValueError("Cohere 模型发现响应中没有 models 数组；请手动填写模型")
            models.update(item["name"] for item in raw["models"]
                          if isinstance(item, dict) and isinstance(item.get("name"), str))
            page_token = raw.get("next_page_token")
            if not page_token:
                return sorted(models)
