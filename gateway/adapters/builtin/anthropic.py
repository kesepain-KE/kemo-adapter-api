from __future__ import annotations

import json

from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind, ProviderResult
from gateway.adapters.builtin.common import DiscoverableAdapter, identifier, message, selected_tools, source_url, system_text, text_blocks, tool_item, tool_call_id, unsupported
from gateway.adapters.contract import AdapterManifest
from gateway.auth import ANTHROPIC_KEY, NO_AUTH
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class Anthropic(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="anthropic-messages", name="Anthropic Messages", operations=("llm",), streaming=True,
        auth_schemes=(ANTHROPIC_KEY, NO_AUTH),
        endpoints={"llm": "/v1/messages", "discovery": "/v1/models"},
        fixed_headers={"anthropic-version": "2023-06-01"},
        media={"input": ("text", "image")},
        body_fields=("model", "messages", "max_tokens", "stream", "system", "temperature", "top_p",
                     "top_k", "stop_sequences", "tools", "tool_choice"),
        usage_semantics="input excludes cache read/write; output includes thinking",
    )
    discovery_path = "/v1/models"

    def encode(self, request, context, model, stream=False):
        messages = []
        for item in request.input:
            if item.type == "message":
                blocks = []
                for block in item.content:
                    if block.type == "text":
                        blocks.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        url = source_url(block, context)
                        if url.startswith("data:"):
                            prefix, data = url.split(",", 1)
                            blocks.append({"type": "image", "source": {"type": "base64", "media_type": prefix[5:].split(";")[0], "data": data}})
                        else:
                            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                    else:
                        raise unsupported("Messages 适配器未声明此媒体操作")
                role = "assistant" if item.role == "assistant" else "user"
            elif item.type == "tool_call":
                role, blocks = "assistant", [{"type": "tool_use", "id": item.call_id, "name": item.name, "input": item.arguments}]
            elif item.type == "tool_result":
                role, blocks = "user", [{"type": "tool_result", "tool_use_id": item.call_id, "content": text_blocks(item), "is_error": item.is_error}]
            else:
                if item.provider_state:
                    raise unsupported("Messages 适配器未声明持久化推理状态")
                continue
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        body = {"model": model, "messages": messages, "max_tokens": request.generation.max_output_tokens or 1024, "stream": stream}
        if system_text(request, context):
            body["system"] = system_text(request, context)
        for key in ("temperature", "top_p", "top_k"):
            if getattr(request.generation, key) is not None:
                body[key] = getattr(request.generation, key)
        if request.generation.stop:
            body["stop_sequences"] = request.generation.stop
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in selected_tools(request)]
            mode = request.tool_choice.mode
            body["tool_choice"] = {"type": "tool", "name": request.tool_choice.name} if mode == "named" else {
                "type": "any" if mode == "required" or (mode == "allowed" and request.tool_choice.allowed_mode == "required") else "none" if mode == "none" else "auto",
            }
            body["tool_choice"]["disable_parallel_tool_use"] = not request.parallel_tool_calls
        return body

    @staticmethod
    def result(raw, ids=None):
        output, ids = [], ids or {}
        phase = "commentary" if any(
            block["type"] == "tool_use" for block in raw.get("content", [])
        ) else "final_answer"
        for index, block in enumerate(raw.get("content", [])):
            if block["type"] == "text" and block.get("text"):
                output.append(message(block["text"], item_id=ids.get(index), phase=phase))
            elif block["type"] == "tool_use":
                arguments = block.get("input", {})
                output.append(tool_item(block["name"], arguments if isinstance(arguments, str) else json.dumps(arguments),
                                        block["id"], item_id=ids.get(index)))
            elif block["type"] == "thinking" and block.get("thinking"):
                output.append({"type": "reasoning", "id": ids.get(index) or identifier("rs"), "content": block["thinking"]})
        if raw.get("stop_reason") == "refusal":
            explanation = (raw.get("stop_details") or {}).get("explanation")
            if explanation:
                output.append({"type": "message", "id": ids.get("refusal") or identifier("msg"),
                               "role": "assistant", "phase": phase, "refusal": explanation})
        raw_usage = dict(raw.get("usage") or {})
        raw_usage["cached"] = raw_usage.get("cache_read_input_tokens", 0)
        if isinstance(raw_usage.get("input_tokens"), int):
            raw_usage["input_tokens"] += raw_usage.get("cache_creation_input_tokens", 0)
        usage = normalize_usage(raw_usage, input_field="input_tokens", output_field="output_tokens",
                                cache_field="cached", input_includes_cache=False)
        status = "requires_action" if any(i["type"] == "tool_call" for i in output) else "completed"
        incomplete = None
        if raw.get("stop_reason") in {"max_tokens", "refusal", "pause_turn"}:
            status = "incomplete"
            incomplete = {"reason": "output_truncated" if raw["stop_reason"] == "max_tokens" else "upstream_stopped"}
        if not output and status == "completed":
            status, incomplete = "incomplete", {"reason": "empty_output"}
        return ProviderResult(status, output, usage, raw.get("id"), incomplete_details=incomplete)

    async def execute(self, request, context, model, upstream, headers):
        raw = await self.transport.json(upstream, "/v1/messages", headers, body=self.encode(request, context, model))
        if raw.get("type") != "message":
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE")
        return self.result(raw)

    async def stream(self, request, context, model, upstream, headers):
        raw, blocks, ids, stopped = {}, {}, {}, False
        async for event in self.transport.events(upstream, "/v1/messages", headers, self.encode(request, context, model, True)):
            kind, index = event.get("type"), event.get("index")
            if kind == "message_start":
                raw = event["message"]
            elif kind == "content_block_start":
                blocks[index] = dict(event["content_block"])
                ids[index] = identifier({"thinking": "rs", "tool_use": "call"}.get(blocks[index]["type"], "msg"))
                if blocks[index]["type"] == "tool_use":
                    initial = blocks[index].get("input")
                    blocks[index]["input"] = json.dumps(initial) if initial else ""
            elif kind == "content_block_delta":
                delta, block = event["delta"], blocks[index]
                dtype = delta["type"]
                if dtype == "text_delta":
                    block["text"] = block.get("text", "") + delta["text"]
                    yield Event(Kind.TEXT_DELTA, item_id=ids[index], content_index=0, delta=delta["text"],
                                item={"phase": "commentary"} if any(b["type"] == "tool_use" for b in blocks.values()) else None)
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta["thinking"]
                    yield Event(Kind.REASONING_CONTENT_DELTA, item_id=ids[index], delta=delta["thinking"])
                elif dtype == "input_json_delta":
                    block["input"] += delta["partial_json"]
                    yield Event(Kind.TOOL_ARGUMENTS_DELTA, item_id=ids[index],
                                call_id=tool_call_id(block["id"]), name=block["name"], delta=delta["partial_json"])
            elif kind == "message_delta":
                raw.update(event["delta"])
                raw.setdefault("usage", {}).update(event.get("usage", {}))
                if raw.get("stop_reason") == "refusal":
                    explanation = (raw.get("stop_details") or {}).get("explanation")
                    if explanation:
                        ids["refusal"] = identifier("msg")
                        yield Event(Kind.REFUSAL_DELTA, item_id=ids["refusal"], delta=explanation)
                        yield Event(Kind.REFUSAL_DONE, item_id=ids["refusal"], text=explanation)
            elif kind == "message_stop":
                stopped = True
            elif kind == "error":
                raise upstream_error(code="UPSTREAM_STREAM_ERROR")
        if not stopped:
            raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")
        raw["content"] = [blocks[i] for i in sorted(blocks)]
        result = self.result(raw, ids)
        for item in result.output:
            if item["type"] == "tool_call":
                yield Event(Kind.TOOL_COMPLETED, item_id=item["id"], item=item, call_id=item["call_id"], name=item["name"])
        yield Event(Kind.USAGE, usage=result.usage)
        yield Event(Kind.INCOMPLETE if result.status == "incomplete" else Kind.COMPLETED, result=result)
