from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

from core.models import ErrorObject
from core.provider_contract import ProviderException
from core.tool_arguments import parse_tool_arguments
from gateway.adapters.contract import AdapterManifest


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


def tool_call_id(upstream_id):
    return "callid_" + hashlib.sha256(upstream_id.encode()).hexdigest()[:24]


def tool_item(name, arguments, upstream_id, *, item_id=None):
    parsed = parse_tool_arguments(arguments)
    return {
        "type": "tool_call", "id": item_id or identifier("call"),
        "call_id": tool_call_id(upstream_id),
        "name": name, "arguments": parsed.arguments,
        "arguments_raw": parsed.arguments_raw, "parse_error": parsed.parse_error,
    }


def message(text, *, item_id=None, phase="final_answer"):
    return {
        "type": "message", "id": item_id or identifier("msg"), "role": "assistant",
        "phase": phase, "content": [{"type": "text", "text": text}],
    }


def unsupported(message):
    return ProviderException(ErrorObject(
        type="unsupported_operation", code="CAPABILITY_ERROR", message=message,
    ))


def text_blocks(item):
    return "\n".join(
        b.text if b.type == "text" else json.dumps(b.data, ensure_ascii=False)
        for b in item.content if b.type in {"text", "json"}
    )


def system_text(request, context):
    return "\n\n".join(s for s in (context.gateway_system_prompt, request.system_prompt) if s)


def source_url(block, context):
    source = block.source
    asset_id = block.asset_id or (source.uri if source is not None and source.kind == "object_store" else None)
    if asset_id and context.assets is not None:
        asset = context.assets.resolve(asset_id)
        data = base64.b64encode(asset.path.read_bytes()).decode()
        return f"data:{asset.descriptor.mime_type};base64,{data}"
    if source is not None and source.kind in {"url", "data_url"}:
        return source.uri
    if source is not None and source.kind == "inline_base64":
        return f"data:{block.mime_type};base64,{source.data}"
    raise unsupported("当前适配器不能读取此媒体来源")


def openai_messages(request, context, *, responses=False, media_inputs=False):
    result = []
    system = system_text(request, context)
    if system:
        result.append({"role": "system", "content": system})
    for item in request.input:
        if item.type == "message":
            content = []
            for block in item.content:
                if block.type == "text":
                    content.append({"type": "input_text" if responses else "text", "text": block.text})
                elif block.type == "image":
                    url = source_url(block, context)
                    content.append({"type": "input_image", "image_url": url} if responses else {
                        "type": "image_url", "image_url": {"url": url},
                    })
                elif media_inputs and block.type == "audio":
                    url = source_url(block, context)
                    if not url.startswith("data:"):
                        raise unsupported("音频输入需要先上传 Asset 或使用内联数据")
                    prefix, data = url.split(",", 1)
                    audio_format = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3"}.get(prefix[5:].split(";")[0])
                    if audio_format is None:
                        raise unsupported("当前 Chat 协议仅支持 WAV 或 MP3 音频输入")
                    content.append({"type": "input_audio", "input_audio": {"data": data, "format": audio_format}})
                elif media_inputs and block.type == "file":
                    url = source_url(block, context)
                    if not url.startswith("data:"):
                        raise unsupported("文件输入需要先上传 Asset 或使用内联数据")
                    content.append({"type": "input_file", "filename": block.filename or "document", "file_data": url}
                                   if responses else {"type": "file", "file": {
                                       "filename": block.filename or "document", "file_data": url}})
                else:
                    raise unsupported("当前文本适配器未声明此媒体操作")
            if responses and item.role == "assistant":
                content = [{"type": "output_text", "text": c["text"]} for c in content if c["type"] == "input_text"]
            result.append({"role": item.role, "content": content})
        elif item.type == "tool_call":
            if responses:
                result.append({"type": "function_call", "call_id": item.call_id,
                               "name": item.name, "arguments": json.dumps(item.arguments)})
            else:
                result.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": item.call_id, "type": "function",
                    "function": {"name": item.name, "arguments": json.dumps(item.arguments)},
                }]})
        elif item.type == "tool_result":
            result.append({"type": "function_call_output", "call_id": item.call_id, "output": text_blocks(item)}
                          if responses else {"role": "tool", "tool_call_id": item.call_id, "content": text_blocks(item)})
        elif item.type == "reasoning":
            # Persisted opaque state needs a protocol-specific implementation.
            if item.provider_state:
                raise unsupported("当前适配器未声明持久化推理状态")
    if responses:
        return result

    # Chat requires all tool results immediately after their assistant turn.
    ordered, deferred, pending = [], [], set()
    for msg in result:
        if msg.get("tool_calls"):
            if pending:
                tool_turn["tool_calls"].extend(msg["tool_calls"])
            else:
                tool_turn = msg
                ordered.append(msg)
            pending.update(call["id"] for call in msg["tool_calls"])
        elif msg["role"] == "tool":
            ordered.append(msg)
            pending.discard(msg["tool_call_id"])
            if not pending:
                ordered.extend(deferred)
                deferred.clear()
        elif pending:
            deferred.append(msg)
        else:
            ordered.append(msg)
    ordered.extend(deferred)
    return ordered


def selected_tools(request):
    allowed = request.tool_choice.allowed_tools
    return [t for t in request.tools if allowed is None or t.name in allowed]


def tool_choice(request, *, responses=False):
    choice = request.tool_choice
    if choice.mode == "named":
        return {"type": "function", "name": choice.name} if responses else {"type": "function", "function": {"name": choice.name}}
    return choice.allowed_mode if choice.mode == "allowed" else choice.mode


class DiscoverableAdapter:
    discovery_path = "/models"

    def __init__(self, transport):
        self.transport = transport

    async def discover(self, upstream, headers):
        value = await self.transport.json(
            upstream, self.manifest.endpoints.get("discovery", self.discovery_path), headers, method="GET",
        )
        data = value.get("data")
        if not isinstance(data, list):
            raise ValueError("模型发现响应中没有 data 数组；请手动填写模型或选择正确协议")
        return sorted({item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)})

    async def list_models(self, upstream, headers):
        return await self.discover(upstream, headers)

    async def describe_model(self, upstream, model, headers):
        """Return normalized metadata; the probe never interprets vendor fields."""
        if not self.manifest.discovery:
            return {}
        value = await self.transport.json(
            upstream, self.manifest.endpoints.get("discovery", self.discovery_path), headers, method="GET",
        )
        raw = next((item for item in value.get("data", value.get("models", []))
                    if isinstance(item, dict) and item.get("id", item.get("name")) == model), {})
        top = raw.get("top_provider") or {}
        limits = {
            "context_window": raw.get("context_length", raw.get("context_window")),
            "max_input_tokens": raw.get("max_input_tokens"),
            "max_output_tokens": raw.get("max_output_tokens", top.get("max_completion_tokens")),
        }
        return {"limits": {key: value for key, value in limits.items()
                           if isinstance(value, int) and not isinstance(value, bool) and value > 0}}
