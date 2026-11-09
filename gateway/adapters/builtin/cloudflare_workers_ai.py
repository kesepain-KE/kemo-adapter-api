from __future__ import annotations

from urllib.parse import quote

from core.provider_contract import ProviderResult
from gateway.adapters.builtin.common import DiscoverableAdapter, message, openai_messages, unsupported
from gateway.adapters.contract import AdapterManifest
from gateway.auth import BEARER, NO_AUTH
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class CloudflareWorkersAI(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="cloudflare-workers-ai", name="Cloudflare Workers AI", operations=("llm",),
        streaming=False, discovery=False, auth_schemes=(BEARER, NO_AUTH),
        endpoints={"llm": "/run"}, tools=False,
        usage_semantics="result.usage provider token counts; absent usage remains unknown",
    )

    def encode(self, request, context):
        # TODO(verify): validate native streaming, tools and model-specific decoding
        # with real credentials before exposing them through this adapter.
        if request.tools or request.structured_output:
            raise unsupported("Workers AI 适配器尚未开放工具和结构化输出")
        messages = openai_messages(request, context)
        for item in messages:
            if item["role"] == "tool" or item.get("tool_calls"):
                raise unsupported("Workers AI 适配器尚未开放工具历史")
            if isinstance(item["content"], list):
                if any(block["type"] != "text" for block in item["content"]):
                    raise unsupported("Workers AI 适配器当前只支持文本输入")
                item["content"] = "\n".join(block["text"] for block in item["content"])
        body = {"messages": messages, "stream": False}
        if request.generation.max_output_tokens:
            body["max_tokens"] = request.generation.max_output_tokens
        if request.generation.temperature is not None:
            body["temperature"] = request.generation.temperature
        return body

    async def execute(self, request, context, model, upstream, headers):
        raw = await self.transport.json(
            upstream, "/run/" + quote(model, safe=""), headers, body=self.encode(request, context),
        )
        if raw.get("success") is False:
            # Provider errors may echo secrets; retain only the normalized error.
            raise upstream_error(code="UPSTREAM_API_ERROR")
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("response"), str):
            raise upstream_error(code="UPSTREAM_INVALID_RESPONSE")
        text = result["response"]
        usage = normalize_usage(result.get("usage"), input_field="prompt_tokens", output_field="completion_tokens")
        return ProviderResult(
            "completed" if text else "incomplete", [message(text)] if text else [], usage,
            incomplete_details=None if text else {"reason": "empty_output"},
        )
