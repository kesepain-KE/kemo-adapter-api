from __future__ import annotations

from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind, ProviderResult
from gateway.adapters.builtin.common import DiscoverableAdapter, identifier, message, openai_messages, selected_tools, tool_choice, tool_item, tool_call_id
from gateway.adapters.contract import AdapterManifest
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class OpenAIResponses(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="openai-responses", name="OpenAI Responses", operations=("llm",), streaming=True,
        usage_semantics="input includes cache; output includes reasoning",
        endpoints={"llm": "/responses", "discovery": "/models"},
        media={"input": ("text", "image")}, structured_output=True,
        reasoning_returns=("none", "summary", "auto"),
        reasoning_levels=("minimal", "low", "medium", "high", "xhigh", "max"),
        body_fields=("model", "input", "stream", "store", "max_output_tokens", "temperature", "top_p",
                     "tools", "tool_choice", "parallel_tool_calls", "text", "reasoning"),
    )

    def encode(self, request, context, model, stream=False):
        body = {"model": model, "input": openai_messages(request, context, responses=True),
                "stream": stream, "store": False}
        for name in ("max_output_tokens", "temperature", "top_p"):
            value = getattr(request.generation, name)
            if value is not None:
                body[name] = value
        if request.tools:
            body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                              "parameters": t.parameters, "strict": t.strict} for t in selected_tools(request)]
            body["tool_choice"] = tool_choice(request, responses=True)
            body["parallel_tool_calls"] = request.parallel_tool_calls
        if request.structured_output:
            config = request.structured_output
            body["text"] = {"format": {"type": config.type}}
            if config.type == "json_schema":
                body["text"]["format"].update(name=config.schema_name, schema=config.schema_, strict=config.strict)
        if request.reasoning and request.reasoning.enabled:
            body["reasoning"] = {"effort": request.reasoning.effort}
            if request.reasoning.return_mode in {"summary", "auto"}:
                body["reasoning"]["summary"] = "auto"
        return body

    @staticmethod
    def result(raw, ids=None):
        ids = ids or {}
        output = []
        default_phase = "commentary" if any(
            item["type"] == "function_call" for item in raw.get("output", [])
        ) else "final_answer"
        for item in raw.get("output", []):
            item_id = ids.get(item.get("id"))
            if item["type"] == "message":
                text = "".join(c["text"] for c in item.get("content", []) if c["type"] == "output_text")
                refusal = next((c["refusal"] for c in item.get("content", []) if c["type"] == "refusal"), None)
                phase = item.get("phase") or default_phase
                if text:
                    output.append(message(text, item_id=item_id, phase=phase))
                    if refusal:
                        output[-1]["refusal"] = refusal
                elif refusal:
                    output.append({"type": "message", "id": item_id or identifier("msg"), "role": "assistant",
                                   "phase": phase, "refusal": refusal})
            elif item["type"] == "function_call":
                output.append(tool_item(item["name"], item.get("arguments"), item["call_id"], item_id=item_id))
            elif item["type"] == "reasoning":
                summary = "\n".join(s["text"] for s in item.get("summary", []) if s.get("text"))
                if summary:
                    output.append({"type": "reasoning", "id": item_id or identifier("rs"), "summary": summary})
        usage = normalize_usage(raw.get("usage"), input_field="input_tokens", output_field="output_tokens",
                                cache_field="input_tokens_details.cached_tokens", reasoning_field="output_tokens_details.reasoning_tokens")
        status = "requires_action" if any(i["type"] == "tool_call" for i in output) else "completed"
        incomplete = None
        if raw.get("status") == "incomplete":
            status = "incomplete"
            reason = (raw.get("incomplete_details") or {}).get("reason")
            incomplete = {"reason": "output_truncated" if reason == "max_output_tokens" else "content_filtered" if reason == "content_filter" else "upstream_stopped"}
        if not output and status == "completed":
            status, incomplete = "incomplete", {"reason": "empty_output"}
        return ProviderResult(status, output, usage, raw.get("id"), incomplete_details=incomplete)

    async def execute(self, request, context, model, upstream, headers):
        raw = await self.transport.json(upstream, "/responses", headers, body=self.encode(request, context, model))
        if raw.get("status") not in {"completed", "incomplete"}:
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE")
        return self.result(raw)

    async def stream(self, request, context, model, upstream, headers):
        ids, items, result = {}, {}, None
        async for event in self.transport.events(upstream, "/responses", headers, self.encode(request, context, model, True)):
            kind = event.get("type")
            raw_id = event.get("item_id")
            if kind in {"response.output_item.added", "response.output_item.done"}:
                item = event["item"]
                raw_id = item["id"]
                items[raw_id] = item
                ids.setdefault(raw_id, identifier({
                    "function_call": "call", "reasoning": "rs",
                }.get(item["type"], "msg")))
                if kind == "response.output_item.added" and item["type"] == "function_call" and item.get("arguments"):
                    yield Event(Kind.TOOL_ARGUMENTS_DELTA, item_id=ids[raw_id],
                                call_id=tool_call_id(item["call_id"]), name=item["name"], delta=item["arguments"])
            elif kind == "response.output_text.delta":
                item_id = ids.setdefault(raw_id, identifier("msg"))
                phase = items.get(raw_id, {}).get("phase")
                yield Event(Kind.TEXT_DELTA, item_id=item_id, content_index=0, delta=event["delta"],
                            item={"phase": phase} if phase else None)
            elif kind == "response.function_call_arguments.delta":
                item = items[raw_id]
                yield Event(Kind.TOOL_ARGUMENTS_DELTA, item_id=ids[raw_id],
                            call_id=tool_call_id(item["call_id"]), name=item["name"], delta=event["delta"])
            elif kind == "response.refusal.delta":
                item_id = ids.setdefault(raw_id, identifier("msg"))
                phase = items.get(raw_id, {}).get("phase")
                yield Event(Kind.REFUSAL_DELTA, item_id=item_id, delta=event["delta"],
                            item={"phase": phase} if phase else None)
            elif kind == "response.refusal.done":
                yield Event(Kind.REFUSAL_DONE, item_id=ids.setdefault(raw_id, identifier("msg")), text=event["refusal"])
            elif kind == "response.reasoning_summary_text.delta":
                item_id = ids.setdefault(raw_id, identifier("rs"))
                yield Event(Kind.REASONING_SUMMARY_DELTA, item_id=item_id, delta=event["delta"])
            elif kind in {"response.completed", "response.incomplete"}:
                result = self.result(event["response"], ids)
            elif kind in {"error", "response.failed"}:
                raise upstream_error(code="UPSTREAM_STREAM_ERROR")
        if result is None:
            raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")
        for item in result.output:
            if item["type"] == "tool_call":
                yield Event(Kind.TOOL_COMPLETED, item_id=item["id"], call_id=item["call_id"], name=item["name"], item=item)
        yield Event(Kind.USAGE, usage=result.usage)
        yield Event(Kind.INCOMPLETE if result.status == "incomplete" else Kind.COMPLETED, result=result)
