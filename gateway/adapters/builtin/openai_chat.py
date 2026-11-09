from __future__ import annotations

from contextlib import aclosing
from dataclasses import replace
import logging

from core.models import Usage
from core.provider_contract import (
    ProviderBatchResult, ProviderChoiceResult, ProviderEvent as Event,
    ProviderEventKind as Kind, ProviderException, ProviderResult,
)
from gateway.adapters.builtin.common import (
    DiscoverableAdapter, identifier, message, openai_messages, selected_tools, tool_choice, tool_item, tool_call_id,
)
from gateway.adapters.contract import AdapterManifest
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


logger = logging.getLogger(__name__)
_TOKEN_FIELD_ERROR_HINTS = (
    "max_tokens", "max_completion_tokens", "unknown_parameter",
    "unrecognized", "unsupported_parameter", "invalid_request_error",
)


class OpenAIChat(DiscoverableAdapter):
    default_reasoning_effort_map = {effort: effort for effort in ("minimal", "low", "medium", "high", "xhigh", "max")}
    endpoint = "/chat/completions"
    manifest = AdapterManifest(
        id="openai-chat", name="OpenAI Chat Completions", operations=("llm",),
        streaming=True, usage_semantics="prompt includes cache; completion includes reasoning",
        endpoints={"llm": "/chat/completions", "discovery": "/models"},
        media={"input": ("text", "image", "audio", "file")},
        multiple_choices=True, structured_output=True,
        reasoning_returns=("none", "content", "auto"),
        reasoning_levels=tuple(default_reasoning_effort_map),
        config_schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "reasoning_effort_map": {"type": "object", "additionalProperties": {"type": "string"}},
                "thinking_mode": {"type": "boolean"},
                "max_tokens_field": {"enum": ["max_tokens", "max_completion_tokens"]},
            },
        },
    )

    @classmethod
    def reasoning_efforts(cls, upstream):
        return upstream.profile.get("reasoning_effort_map", cls.default_reasoning_effort_map)

    async def describe_model(self, upstream, model, headers):
        value = await self.transport.json(upstream, self.manifest.endpoints["discovery"], headers, method="GET")
        raw = next((item for item in value.get("data", [])
                    if isinstance(item, dict) and item.get("id") == model), {})
        top = raw.get("top_provider") or {}
        limits = {"context_window": raw.get("context_length", raw.get("context_window")),
                  "max_input_tokens": raw.get("max_input_tokens"),
                  "max_output_tokens": raw.get("max_output_tokens", top.get("max_completion_tokens"))}
        effort = raw.get("effort")
        levels = effort.get("supported_levels", []) if isinstance(effort, dict) else []
        return {
            "limits": {key: value for key, value in limits.items()
                       if isinstance(value, int) and not isinstance(value, bool) and value > 0},
            "reasoning_efforts": [effort for effort, value in self.reasoning_efforts(upstream).items()
                                  if value in levels],
        }

    def encode(self, request, context, model, upstream, stream=False):
        body = {"model": model, "messages": openai_messages(
            request, context, media_inputs=bool(set(self.manifest.media.input) - {"text", "image"}),
        ), "stream": stream}
        for name in ("temperature", "top_p", "stop", "seed", "presence_penalty", "frequency_penalty", "logit_bias"):
            value = getattr(request.generation, name)
            if value is not None:
                body[name] = value
        if request.generation.n > 1:
            body["n"] = request.generation.n
        if request.generation.max_output_tokens:
            body[upstream.profile.get("max_tokens_field", "max_tokens")] = request.generation.max_output_tokens
        if request.tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters, "strict": t.strict,
            }} for t in selected_tools(request)]
            body["tool_choice"] = tool_choice(request)
            body["parallel_tool_calls"] = request.parallel_tool_calls
        if request.structured_output:
            structured = request.structured_output
            body["response_format"] = {"type": structured.type}
            if structured.type == "json_schema":
                body["response_format"]["json_schema"] = {
                    "name": structured.schema_name, "schema": structured.schema_, "strict": structured.strict,
                }
        if request.reasoning is not None:
            if upstream.profile.get("thinking_mode"):
                body["thinking"] = {"type": "enabled" if request.reasoning.enabled else "disabled"}
            if request.reasoning.enabled:
                body["reasoning_effort"] = self.reasoning_efforts(upstream)[request.reasoning.effort]
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def usage(raw):
        return normalize_usage(
            raw, input_field="prompt_tokens", output_field="completion_tokens",
            cache_field=("prompt_tokens_details.cached_tokens"
                         if ((raw or {}).get("prompt_tokens_details") or {}).get("cached_tokens") is not None
                         else "prompt_cache_hit_tokens"),
            reasoning_field="completion_tokens_details.reasoning_tokens",
        )

    @staticmethod
    def result(choice, usage, provider_id, *, msg_id=None):
        content = choice.get("message", {})
        phase = "commentary" if content.get("tool_calls") else "final_answer"
        output = []
        if content.get("reasoning_content"):
            output.append({"type": "reasoning", "id": identifier("rs"), "content": content["reasoning_content"]})
        if content.get("content"):
            output.append(message(content["content"], item_id=msg_id, phase=phase))
        if content.get("refusal"):
            if output and output[-1]["type"] == "message":
                output[-1]["refusal"] = content["refusal"]
            else:
                output.append({"type": "message", "id": msg_id or identifier("msg"), "role": "assistant",
                               "phase": phase, "refusal": content["refusal"]})
        output.extend(tool_item(t["function"]["name"], t["function"].get("arguments"), t["id"],
                                item_id=t.get("_item_id")) for t in content.get("tool_calls", []))
        reason = choice.get("finish_reason")
        status = "requires_action" if content.get("tool_calls") else "completed"
        incomplete = None
        if reason in {"length", "content_filter"}:
            status = "incomplete"
            incomplete = {"reason": "output_truncated" if reason == "length" else "content_filtered"}
        if not output and status == "completed":
            status, incomplete = "incomplete", {"reason": "empty_output"}
        return ProviderResult(status=status, output=output, usage=usage,
                              provider_response_id=provider_id, incomplete_details=incomplete)

    async def execute(self, request, context, model, upstream, headers):
        try:
            raw = await self.transport.json(upstream, self.manifest.endpoints["llm"], headers,
                                            body=self.encode(request, context, model, upstream))
        except ProviderException as exc:
            retry_upstream = self._token_field_retry(exc, request, upstream)
            if retry_upstream is None:
                raise
            raw = await self.transport.json(retry_upstream, self.manifest.endpoints["llm"], headers,
                                            body=self.encode(request, context, model, retry_upstream))
        choices = raw.get("choices")
        if not choices:
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE")
        usage = self.usage(raw.get("usage"))
        if request.generation.n > 1:
            return ProviderBatchResult(
                [ProviderChoiceResult(c["index"], self.result(c, usage, raw.get("id"))) for c in choices], usage,
            )
        return self.result(choices[0], usage, raw.get("id"))

    @staticmethod
    def _token_field_retry(exc, request, upstream):
        if exc.error.provider_status != 400 or not request.generation.max_output_tokens:
            return None
        body = (exc.error.details or {}).get("upstream_body", "").lower()
        if not any(hint in body for hint in _TOKEN_FIELD_ERROR_HINTS):
            return None
        current = upstream.profile.get("max_tokens_field", "max_tokens")
        alternative = "max_completion_tokens" if current == "max_tokens" else "max_tokens"
        logger.warning(
            "max_tokens 字段被上游拒绝，回退重试: upstream=%s, from=%s, to=%s",
            upstream.id, current, alternative,
        )
        return replace(upstream, profile={**upstream.profile, "max_tokens_field": alternative})

    async def _events(self, request, context, model, upstream, headers):
        events = self.transport.events(
            upstream, self.manifest.endpoints["llm"], headers, self.encode(request, context, model, upstream, True),
        )
        try:
            try:
                first = await anext(events)
            except ProviderException as exc:
                await events.aclose()
                retry_upstream = self._token_field_retry(exc, request, upstream)
                if retry_upstream is None:
                    raise
                events = self.transport.events(
                    retry_upstream, self.manifest.endpoints["llm"], headers,
                    self.encode(request, context, model, retry_upstream, True),
                )
                first = await anext(events)
            yield first
            async for raw in events:
                yield raw
        except StopAsyncIteration:
            raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM") from None
        finally:
            await events.aclose()

    async def stream(self, request, context, model, upstream, headers):
        msg_id, reasoning_id = identifier("msg"), identifier("rs")
        text, reasoning, refusal, calls = "", "", "", {}
        usage, finished, provider_id, refusal_closed = Usage(), None, None, False
        async with aclosing(self._events(request, context, model, upstream, headers)) as events:
            async for raw in events:
                provider_id = raw.get("id", provider_id)
                if raw.get("error"):
                    raise upstream_error(code="UPSTREAM_STREAM_ERROR")
                if raw.get("usage"):
                    usage = self.usage(raw["usage"])
                    if refusal and not refusal_closed:
                        yield Event(Kind.REFUSAL_DONE, item_id=msg_id, text=refusal)
                        refusal_closed = True
                    yield Event(Kind.USAGE, usage=usage)
                for choice in raw.get("choices", []):
                    if choice.get("index", 0) != 0:
                        raise upstream_error(code="UPSTREAM_UNEXPECTED_CHOICE")
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        text += delta["content"]
                        yield Event(Kind.TEXT_DELTA, item_id=msg_id, content_index=0, delta=delta["content"],
                                    provider_response_id=provider_id,
                                    item={"phase": "commentary"} if calls or delta.get("tool_calls") else None)
                    if delta.get("reasoning_content"):
                        reasoning += delta["reasoning_content"]
                        yield Event(Kind.REASONING_CONTENT_DELTA, item_id=reasoning_id, delta=delta["reasoning_content"])
                    if delta.get("refusal"):
                        refusal += delta["refusal"]
                        yield Event(Kind.REFUSAL_DELTA, item_id=msg_id, delta=delta["refusal"])
                    for call in delta.get("tool_calls", []):
                        target = calls.setdefault(call["index"], {
                            "id": "", "function": {"name": "", "arguments": ""},
                            "_item_id": identifier("call"), "_emitted": 0,
                        })
                        target["id"] += call.get("id", "")
                        target["function"]["name"] += call.get("function", {}).get("name", "")
                        target["function"]["arguments"] += call.get("function", {}).get("arguments", "")
                        arguments = target["function"]["arguments"]
                        if target["id"] and target["function"]["name"] and len(arguments) > target["_emitted"]:
                            yield Event(Kind.TOOL_ARGUMENTS_DELTA, item_id=target["_item_id"],
                                        call_id=tool_call_id(target["id"]), name=target["function"]["name"],
                                        delta=arguments[target["_emitted"]:])
                            target["_emitted"] = len(arguments)
                    finished = choice.get("finish_reason") or finished
        if finished is None:
            raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")
        if refusal and not refusal_closed:
            yield Event(Kind.REFUSAL_DONE, item_id=msg_id, text=refusal)
        result = self.result({"message": {
            "content": text, "reasoning_content": reasoning, "refusal": refusal,
            "tool_calls": list(calls.values()),
        }, "finish_reason": finished}, usage, provider_id, msg_id=msg_id)
        for item in result.output:
            if item["type"] == "reasoning":
                item["id"] = reasoning_id
            elif item["type"] == "tool_call":
                yield Event(Kind.TOOL_COMPLETED, item_id=item["id"], item=item,
                            call_id=item["call_id"], name=item["name"])
        yield Event(Kind.INCOMPLETE if result.status == "incomplete" else Kind.COMPLETED, result=result)
