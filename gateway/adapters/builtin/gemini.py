from __future__ import annotations

import json
from urllib.parse import quote

from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind, ProviderResult
from gateway.adapters.builtin.common import DiscoverableAdapter, identifier, message, selected_tools, source_url, system_text, text_blocks, tool_item, unsupported
from gateway.adapters.contract import AdapterManifest
from gateway.auth import GEMINI_QUERY, GEMINI_KEY, NO_AUTH
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class Gemini(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="gemini", name="Google Gemini", operations=("llm",), streaming=True,
        auth_schemes=(GEMINI_QUERY, GEMINI_KEY, NO_AUTH),
        endpoints={"llm": "/models", "discovery": "/models"},
        media={"input": ("text", "image", "audio", "video", "file")},
        structured_output=True,
        body_fields=("contents", "systemInstruction", "generationConfig", "tools", "toolConfig"),
        usage_semantics="prompt includes cache; candidate excludes thoughts",
    )

    async def discover(self, upstream, headers):
        value = await self.transport.json(upstream, "/models", headers, method="GET")
        if not isinstance(value.get("models"), list):
            raise ValueError("Gemini 模型目录响应无效")
        if value.get("nextPageToken"):
            raise ValueError("模型目录有后续分页，请用模板目录或手动登记目标模型")
        return sorted(m["name"].removeprefix("models/") for m in value["models"]
                      if "generateContent" in m.get("supportedGenerationMethods", []))

    async def describe_model(self, upstream, model, headers):
        raw = await self.transport.json(
            upstream, "/models/" + quote(model.removeprefix("models/"), safe=""), headers, method="GET",
        )
        limits = {"max_input_tokens": raw.get("inputTokenLimit"), "max_output_tokens": raw.get("outputTokenLimit")}
        return {"limits": {key: value for key, value in limits.items()
                           if isinstance(value, int) and not isinstance(value, bool) and value > 0}}

    def encode(self, request, context, model):
        contents = []
        for item in request.input:
            if item.type == "message":
                parts = []
                for block in item.content:
                    if block.type == "text":
                        parts.append({"text": block.text})
                    elif block.type in {"image", "audio", "video", "file"}:
                        url = source_url(block, context)
                        if not url.startswith("data:"):
                            raise unsupported("Gemini 媒体输入需要先上传 Asset 或使用内联数据")
                        prefix, data = url.split(",", 1)
                        parts.append({"inlineData": {"mimeType": prefix[5:].split(";")[0], "data": data}})
                    else:
                        raise unsupported("Gemini 适配器未声明此媒体操作")
                role = "model" if item.role == "assistant" else "user"
            elif item.type == "tool_call":
                role, parts = "model", [{"functionCall": {"name": item.name, "args": item.arguments}}]
            elif item.type == "tool_result":
                role, parts = "user", [{"functionResponse": {"name": item.name, "response": {"result": text_blocks(item)}}}]
            else:
                if item.provider_state:
                    raise unsupported("Gemini 适配器未声明持久化推理状态")
                continue
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        body = {"contents": contents}
        if system_text(request, context):
            body["systemInstruction"] = {"parts": [{"text": system_text(request, context)}]}
        config = {}
        for src, dst in (("max_output_tokens", "maxOutputTokens"), ("temperature", "temperature"),
                         ("top_p", "topP"), ("top_k", "topK"), ("stop", "stopSequences")):
            if getattr(request.generation, src) is not None:
                config[dst] = getattr(request.generation, src)
        if request.structured_output:
            config["responseMimeType"] = "application/json"
            if request.structured_output.schema_:
                config["responseJsonSchema"] = request.structured_output.schema_
        if config:
            body["generationConfig"] = config
        if request.tools:
            body["tools"] = [{"functionDeclarations": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in selected_tools(request)]}]
            mode = request.tool_choice.mode
            function = {"mode": "NONE" if mode == "none" else "ANY" if mode in {"required", "named"} or (
                mode == "allowed" and request.tool_choice.allowed_mode == "required") else "AUTO"}
            if mode == "named":
                function["allowedFunctionNames"] = [request.tool_choice.name]
            elif mode == "allowed":
                function["allowedFunctionNames"] = request.tool_choice.allowed_tools
            body["toolConfig"] = {"functionCallingConfig": function}
        return body

    @staticmethod
    def result(raw, msg_id=None):
        candidate = (raw.get("candidates") or [{}])[0]
        output, text = [], ""
        for index, part in enumerate(candidate.get("content", {}).get("parts", [])):
            if part.get("thought"):
                continue
            if "text" in part:
                text += part["text"]
            if "functionCall" in part:
                call = part["functionCall"]
                output.append(tool_item(call["name"], json.dumps(call.get("args", {})), f'{index}:{call["name"]}'))
        if text:
            output.insert(0, message(
                text, item_id=msg_id, phase="commentary" if output else "final_answer",
            ))
        usage = normalize_usage(raw.get("usageMetadata"), input_field="promptTokenCount",
                                output_field="candidatesTokenCount", cache_field="cachedContentTokenCount",
                                reasoning_field="thoughtsTokenCount", output_includes_reasoning=False)
        status = "requires_action" if any(i["type"] == "tool_call" for i in output) else "completed"
        reason = candidate.get("finishReason")
        incomplete = None
        if reason not in {None, "STOP"}:
            status, incomplete = "incomplete", {"reason": "output_truncated" if reason == "MAX_TOKENS" else "content_filtered"}
        if not output and status == "completed":
            status, incomplete = "incomplete", {"reason": "empty_output"}
        return ProviderResult(status, output, usage, raw.get("responseId"), incomplete_details=incomplete)

    async def execute(self, request, context, model, upstream, headers):
        path = "/models/" + quote(model.removeprefix("models/"), safe="") + ":generateContent"
        raw = await self.transport.json(upstream, path, headers, body=self.encode(request, context, model))
        if "candidates" not in raw and "promptFeedback" not in raw:
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE")
        return self.result(raw)

    async def stream(self, request, context, model, upstream, headers):
        path = "/models/" + quote(model.removeprefix("models/"), safe="") + ":streamGenerateContent"
        path += "?alt=sse"
        msg_id, parts, final = identifier("msg"), [], {}
        stopped = False
        async for raw in self.transport.events(upstream, path, headers, self.encode(request, context, model)):
            candidate = (raw.get("candidates") or [{}])[0]
            for part in candidate.get("content", {}).get("parts", []):
                parts.append(part)
                if part.get("text") and not part.get("thought"):
                    yield Event(Kind.TEXT_DELTA, item_id=msg_id, content_index=0, delta=part["text"])
            if raw.get("usageMetadata"):
                final["usageMetadata"] = raw["usageMetadata"]
            if candidate.get("finishReason"):
                stopped = True
                final["candidates"] = [{"finishReason": candidate["finishReason"]}]
            final["responseId"] = raw.get("responseId")
        if not stopped:
            raise upstream_error(code="UPSTREAM_TRUNCATED_STREAM")
        final["candidates"][0]["content"] = {"parts": parts}
        result = self.result(final, msg_id)
        for item in result.output:
            if item["type"] == "tool_call":
                yield Event(Kind.TOOL_COMPLETED, item_id=item["id"], item=item, call_id=item["call_id"], name=item["name"])
        yield Event(Kind.USAGE, usage=result.usage)
        yield Event(Kind.INCOMPLETE if result.status == "incomplete" else Kind.COMPLETED, result=result)
