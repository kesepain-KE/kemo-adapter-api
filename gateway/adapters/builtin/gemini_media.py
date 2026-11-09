from __future__ import annotations

import asyncio
import io
import re
from urllib.parse import quote, urlsplit
import wave

from core.assets import detect_mime
from core.models import MediaUsage, Usage
from core.provider_contract import ProviderException
from gateway.adapters.builtin.common import source_url, unsupported
from gateway.adapters.builtin.gemini import Gemini
from gateway.adapters.builtin.media import decoded, download, inputs, prompt, result, store
from gateway.adapters.contract import AdapterManifest
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class GeminiSpeech(Gemini):
    manifest = AdapterManifest(
        id="gemini-speech", name="Gemini Speech", operations=("llm",),
        tools=False, auth_schemes=Gemini.manifest.auth_schemes,
        media={"input": ("text",), "output": ("audio",)}, probe_operation="speech_generation",
        endpoints=Gemini.manifest.endpoints, body_fields=Gemini.manifest.body_fields,
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "voice": {"type": "string", "minLength": 1},
        }},
        usage_semantics="Gemini prompt/candidate token usage; PCM wrapped in WAV without resampling",
    )

    async def execute(self, request, context, model, upstream, headers):
        if request.metadata.get("capability") != "speech_generation" or request.output.modalities != ["audio"]:
            raise unsupported("Gemini 语音生成需要 speech_generation，输出模态为 audio")
        config = request.output.audio
        if config and config.format != "wav":
            raise unsupported("Gemini 语音输出使用 wav")
        body = self.encode(request, context, model)
        body["generationConfig"] = {"responseModalities": ["AUDIO"], "speechConfig": {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName":
                config.voice if config and config.voice != "default" else upstream.profile.get("voice", "Kore")}}}}
        raw = await self.transport.json(upstream, "/models/" + quote(model.removeprefix("models/"), safe="")
                                        + ":generateContent", headers, body=body)
        audio = [part["inlineData"] for candidate in raw.get("candidates", [])
                 for part in candidate.get("content", {}).get("parts", []) if "inlineData" in part]
        if len(audio) != 1:
            raise upstream_error(code="UPSTREAM_INVALID_AUDIO_RESPONSE", retryable=False)
        content = decoded(audio[0].get("data"))
        mime = audio[0].get("mimeType", "")
        matched = re.fullmatch(r"audio/L16;codec=pcm;rate=(\d+)", mime, flags=re.IGNORECASE)
        duration = None
        if matched:
            rate = int(matched[1])
            if rate < 8000 or rate > 192000 or len(content) % 2:
                raise upstream_error(code="UPSTREAM_INVALID_AUDIO_CONTENT", retryable=False)
            output = io.BytesIO()
            with wave.open(output, "wb") as wave_file:
                wave_file.setnchannels(1)
                wave_file.setsampwidth(2)
                wave_file.setframerate(rate)
                wave_file.writeframes(content)
            duration = len(content) / (rate * 2)
            content = output.getvalue()
        elif detect_mime(content[:32]) != "audio/wav":
            raise upstream_error(code="UPSTREAM_INVALID_AUDIO_CONTENT", retryable=False)
        block = await store(context, "audio", filename="speech.wav", mime_type="audio/wav", content=content)
        usage = normalize_usage(raw.get("usageMetadata"), input_field="promptTokenCount",
                                output_field="candidatesTokenCount", cache_field="cachedContentTokenCount")
        usage.media = MediaUsage(output_audio_seconds=duration)
        usage.provider_raw = raw.get("usageMetadata") or {}
        return result([block], usage, response_id=raw.get("responseId"))


class GeminiVideo(Gemini):
    manifest = AdapterManifest(
        id="gemini-video", name="Gemini Video Jobs", operations=("llm",),
        tools=False, auth_schemes=Gemini.manifest.auth_schemes,
        media={"input": ("text", "image"), "output": ("video",)}, probe_operation="video_generation",
        endpoints=Gemini.manifest.endpoints,
        body_fields=("instances", "parameters"),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "aspect_ratio": {"enum": ["16:9", "9:16"]},
            "poll_interval": {"type": "number", "minimum": 0.1, "maximum": 30},
        }},
        usage_semantics="duration is not inferred from the requested value; provider usage retained if present",
    )

    async def discover(self, upstream, headers):
        raw = await self.transport.json(upstream, "/models", headers, method="GET")
        if not isinstance(raw.get("models"), list) or raw.get("nextPageToken"):
            raise ValueError("视频模型目录无效或有后续分页，请手动登记目标模型")
        return sorted(model["name"].removeprefix("models/") for model in raw["models"]
                      if set(model.get("supportedGenerationMethods", [])) & {"predictLongRunning", "generateVideos"})

    async def execute(self, request, context, model, upstream, headers):
        if request.metadata.get("capability") != "video_generation" or request.output.modalities != ["video"]:
            raise unsupported("Gemini 视频生成需要 video_generation，输出模态为 video")
        config = request.output.video
        if config and config.format != "mp4":
            raise unsupported("Gemini 视频任务输出使用 mp4")
        instance = {"prompt": prompt(request, context)}
        parameters = {"numberOfVideos": 1, "aspectRatio": upstream.profile.get("aspect_ratio", "16:9")}
        if config and config.duration_seconds is not None:
            if not config.duration_seconds.is_integer():
                raise unsupported("视频时长必须为整数秒")
            parameters["durationSeconds"] = int(config.duration_seconds)
        images = inputs(request, "image")
        if len(images) > 1:
            raise unsupported("Gemini 视频生成最多一个参考图片")
        if images:
            url = source_url(images[0], context)
            if not url.startswith("data:"):
                raise unsupported("参考图片需要 Asset 或内联数据")
            prefix, data = url.split(",", 1)
            instance["image"] = {"bytesBase64Encoded": data, "mimeType": prefix[5:].split(";")[0]}
        raw = await self.transport.json(
            upstream, "/models/" + quote(model.removeprefix("models/"), safe="") + ":predictLongRunning",
            headers, body={"instances": [instance], "parameters": parameters},
        )
        name = raw.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"models/[A-Za-z0-9_.-]+/operations/[A-Za-z0-9_-]+", name):
            raise upstream_error(code="UPSTREAM_INVALID_VIDEO_JOB", retryable=False)
        try:
            while not raw.get("done"):
                await asyncio.sleep(upstream.profile.get("poll_interval", 2))
                raw = await self.transport.json(upstream, "/" + name, headers, method="GET")
                if raw.get("name") != name:
                    raise upstream_error(code="UPSTREAM_INVALID_VIDEO_JOB", retryable=False)
            samples = (raw.get("response") or {}).get("generateVideoResponse", {}).get("generatedSamples", [])
            if raw.get("error") or len(samples) != 1:
                raise upstream_error(code="UPSTREAM_VIDEO_JOB_FAILED", retryable=False)
            uri = samples[0].get("video", {}).get("uri")
            parsed, base = urlsplit(uri or ""), urlsplit(upstream.base_url)
            relative = parsed.path.removeprefix(base.path.rstrip("/") + "/")
            if ((parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or parsed.query not in {"", "alt=media"} or parsed.fragment
                    or not re.fullmatch(r"files/[A-Za-z0-9_-]+(?::download)?", relative)):
                raise upstream_error(code="UPSTREAM_UNSAFE_VIDEO_URI", retryable=False)
            path = "/" + relative.removesuffix(":download") + ":download?alt=media"
            block = await download(self.transport, upstream, path, headers, context, "video",
                                   filename="video.mp4", mime_type="video/mp4", metadata={"provider_job_id": name})
        except (ProviderException, TimeoutError) as exc:
            error = exc if isinstance(exc, ProviderException) else upstream_error(code="UPSTREAM_TIMEOUT", retryable=False)
            error.error.retryable = False
            error.error.details = {**(error.error.details or {}), "provider_job_id": name}
            raise error from None
        return result([block], Usage(provider_raw=raw.get("usageMetadata") or {}), response_id=name)
