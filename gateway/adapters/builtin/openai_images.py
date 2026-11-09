from __future__ import annotations

import base64
from contextlib import ExitStack

from core.assets import detect_mime
from core.models import MediaUsage
from core.provider_contract import ProviderResult
from gateway.adapters.builtin.common import DiscoverableAdapter, identifier, system_text, text_blocks, unsupported
from gateway.adapters.contract import AdapterManifest
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class OpenAIImages(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="openai-images", name="OpenAI Images", version="1.1.0", operations=("llm",),
        streaming=False, tools=False, media={"input": ("text", "image"), "output": ("image",)},
        probe_operation="image_generation",
        endpoints={"llm": "/images/generations", "image_edit": "/images/edits", "discovery": "/models"},
        body_fields=("model", "prompt", "n", "size", "output_format", "image", "image[]", "mask",
                     "quality", "background", "moderation", "output_compression", "response_format"),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "quality": {"type": "string", "minLength": 1},
            "background": {"enum": ["auto", "transparent", "opaque"]},
            "moderation": {"enum": ["auto", "low"]},
            "output_compression": {"type": "integer", "minimum": 0, "maximum": 100},
            "response_format": {"const": "b64_json"},
        }},
        usage_semantics="provider input/output tokens include image tokens; output_images counted from decoded assets",
    )

    async def execute(self, request, context, model, upstream, headers):
        operation = request.metadata.get("capability")
        if operation not in {"image_generation", "image_edit"} or request.output.modalities != ["image"]:
            raise unsupported("图片适配器需要 image_generation 或 image_edit，且输出模态为 image")
        if context.assets is None:
            raise unsupported("图片输出需要网关 Asset 存储")
        config = request.output.image
        if config is None or config.format not in {"png", "jpeg", "webp"}:
            raise unsupported("图片输出格式仅支持 png、jpeg 和 webp")
        prompt = "\n\n".join(filter(None, [system_text(request, context), *[
            text_blocks(item) for item in request.input if item.type == "message" and item.role == "user"
        ]]))
        if not prompt:
            raise unsupported("图片请求需要文本提示词")
        images = [block for item in request.input if item.type == "message"
                  for block in item.content if block.type == "image"]
        body = {"model": model, "prompt": prompt, "n": 1, "size": config.size}
        if config.format != "png":
            body["output_format"] = config.format
        for key in self.manifest.config_schema["properties"]:
            if key in upstream.profile:
                body[key] = upstream.profile[key]
        if operation == "image_generation":
            if images:
                raise unsupported("有图片输入时请使用 image_edit")
            raw = await self.transport.json(upstream, self.manifest.endpoints["llm"], headers, body=body)
        else:
            if not images:
                raise unsupported("图片编辑需要至少一个已上传的图片 Asset")
            roles = {item.get("asset_id"): item.get("role") for item in
                     request.metadata.get("multimodal", {}).get("assets", [])}
            with ExitStack() as stack:
                files, masks = [], 0
                for index, block in enumerate(images):
                    asset_id = block.asset_id or (
                        block.source.uri if block.source is not None and block.source.kind == "object_store" else None
                    )
                    if not asset_id:
                        raise unsupported("图片编辑请先上传图片，再使用 asset_id 引用")
                    asset = context.assets.resolve(asset_id)
                    if not asset.descriptor.mime_type.startswith("image/"):
                        raise unsupported("图片编辑 Asset 必须为图片")
                    mask = roles.get(asset_id) == "mask"
                    masks += mask
                    if masks > 1:
                        raise unsupported("图片编辑只能提供一个 mask")
                    files.append(("mask" if mask else "image[]", (
                        f"input-{index}", stack.enter_context(asset.path.open("rb")), asset.descriptor.mime_type,
                    )))
                if all(name == "mask" for name, _ in files):
                    raise unsupported("图片编辑缺少源图片")
                raw = await self.transport.json(
                    upstream, self.manifest.endpoints["image_edit"], headers, body=body, files=files,
                )
        data = raw.get("data")
        if not isinstance(data, list) or not data:
            raise upstream_error(code="UPSTREAM_INVALID_IMAGE_RESPONSE", retryable=False)
        # Decode the complete response before publishing any output Asset.
        decoded = []
        for item in data:
            try:
                content = base64.b64decode(item["b64_json"], validate=True)
            except (KeyError, TypeError, ValueError):
                raise upstream_error(code="UPSTREAM_INVALID_IMAGE_BASE64", retryable=False) from None
            mime = detect_mime(content[:32])
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise upstream_error(code="UPSTREAM_INVALID_IMAGE_CONTENT", retryable=False)
            decoded.append((content, mime, item.get("revised_prompt")))
        output = []
        for index, (content, mime, revised_prompt) in enumerate(decoded):
            asset = await context.assets.store_output(
                filename=f"image-{index}.{mime.split('/')[1]}", mime_type=mime, content=content,
                metadata={"request_id": context.request_id, "capability": operation,
                          **({"revised_prompt": revised_prompt} if isinstance(revised_prompt, str) else {})},
            )
            output.append({"type": "image", "asset_id": asset.id, "mime_type": asset.mime_type,
                           "checksum_sha256": asset.checksum_sha256})
        usage = normalize_usage(raw.get("usage"), input_field="input_tokens", output_field="output_tokens",
                                cache_field="input_tokens_details.cached_tokens")
        usage.media = MediaUsage(input_images=len(images), output_images=len(output))
        if isinstance(raw.get("usage"), dict):
            usage.provider_raw = raw["usage"]
        return ProviderResult(
            status="completed",
            output=[{"type": "message", "id": identifier("msg"), "role": "assistant",
                     "phase": "final_answer", "content": output}],
            usage=usage,
        )
