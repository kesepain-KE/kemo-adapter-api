from __future__ import annotations

import asyncio

from core.models import MediaUsage, Usage
from core.provider_contract import ProviderException
from gateway.adapters.builtin.common import DiscoverableAdapter, unsupported
from gateway.adapters.builtin.media import download, inputs, native_id, owned_input, prompt, result
from gateway.adapters.contract import AdapterManifest
from gateway.transport import upstream_error


class OpenAIVideo(DiscoverableAdapter):
    """Job-based /videos protocol used by compatible upstream services."""
    manifest = AdapterManifest(
        id="openai-video", name="OpenAI-compatible Video Jobs", operations=("llm",),
        tools=False, media={"input": ("text", "image"), "output": ("video",)},
        probe_operation="video_generation",
        endpoints={"llm": "/videos", "discovery": "/models"},
        body_fields=("model", "prompt", "seconds", "size", "input_reference"),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "size": {"type": "string", "pattern": r"^[1-9]\d*x[1-9]\d*$"},
            "poll_interval": {"type": "number", "minimum": 0.1, "maximum": 30},
        }},
        usage_semantics="output_video_seconds from completed upstream job; tokens remain unknown",
    )

    async def execute(self, request, context, model, upstream, headers):
        if request.metadata.get("capability") != "video_generation" or request.output.modalities != ["video"]:
            raise unsupported("视频生成需要 video_generation，输出模态为 video")
        config = request.output.video
        if config and config.format != "mp4":
            raise unsupported("视频任务输出仅支持 mp4")
        duration = config.duration_seconds if config and config.duration_seconds is not None else 4
        if duration not in {4, 8, 12}:
            raise unsupported("此视频任务协议时长支持 4、8、12 秒")
        body = {"model": model, "prompt": prompt(request, context), "seconds": str(int(duration)),
                "size": upstream.profile.get("size", "1280x720")}
        if not body["prompt"]:
            raise unsupported("视频生成需要文本提示词")
        images = inputs(request, "image")
        if len(images) > 1:
            raise unsupported("视频生成最多一个参考图片 Asset")
        if images:
            asset = owned_input(images[0], context)
            with asset.path.open("rb") as source:
                raw = await self.transport.json(upstream, "/videos", headers, body=body, files=[
                    ("input_reference", (asset.descriptor.filename, source, asset.descriptor.mime_type)),
                ])
        else:
            # File tuples with no filename produce multipart form fields.
            raw = await self.transport.json(upstream, "/videos", headers, files=[
                (name, (None, value, "text/plain")) for name, value in body.items()
            ])
        identity = native_id(raw.get("id"))
        try:
            while raw.get("status") in {"queued", "in_progress"}:
                await asyncio.sleep(upstream.profile.get("poll_interval", 2))
                raw = await self.transport.json(upstream, "/videos/" + identity, headers, method="GET")
                if raw.get("id") != identity:
                    raise upstream_error(code="UPSTREAM_INVALID_VIDEO_JOB", retryable=False)
            if raw.get("status") != "completed":
                raise upstream_error(code="UPSTREAM_VIDEO_JOB_FAILED", retryable=False)
            block = await download(self.transport, upstream, f"/videos/{identity}/content", headers, context,
                                   "video", filename="video.mp4", mime_type="video/mp4",
                                   metadata={"provider_job_id": identity})
        except (ProviderException, TimeoutError) as exc:
            error = exc if isinstance(exc, ProviderException) else upstream_error(code="UPSTREAM_TIMEOUT", retryable=False)
            error.error.retryable = False
            error.error.details = {**(error.error.details or {}), "provider_job_id": identity}
            raise error from None
        seconds = raw.get("seconds")
        try:
            seconds = float(seconds) if seconds is not None else None
            if seconds is not None and seconds < 0:
                seconds = None
        except (TypeError, ValueError):
            seconds = None
        return result([block], Usage(media=MediaUsage(output_video_seconds=seconds)), response_id=identity)
