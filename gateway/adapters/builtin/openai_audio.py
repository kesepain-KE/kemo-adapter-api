from __future__ import annotations

import base64
from contextlib import aclosing
import tempfile

import httpx

from core.assets import detect_mime
from core.models import MediaUsage, Usage
from core.provider_contract import ProviderEvent as Event, ProviderEventKind as Kind
from gateway.adapters.builtin.common import DiscoverableAdapter, openai_messages, unsupported
from gateway.adapters.builtin.media import (
    audio_seconds, decoded, inputs, owned_input, prompt, result, store,
)
from gateway.adapters.contract import AdapterManifest
from gateway.metering import normalize_usage
from gateway.transport import upstream_error


class OpenAITranscription(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="openai-transcription", name="OpenAI-compatible Audio Transcription", operations=("llm",),
        tools=False, media={"input": ("text", "audio")}, probe_operation="audio_transcription",
        endpoints={"llm": "/audio/transcriptions", "discovery": "/models"},
        body_fields=("model", "file", "response_format", "language", "prompt", "temperature"),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "language": {"type": "string", "minLength": 1},
        }},
        usage_semantics="provider token usage or audio duration; missing values remain unknown",
    )

    async def execute(self, request, context, model, upstream, headers):
        if request.metadata.get("capability") != "audio_transcription" or request.output.modalities != ["text"]:
            raise unsupported("音频转写需要 audio_transcription，输出模态为 text")
        blocks = inputs(request, "audio")
        if len(blocks) != 1:
            raise unsupported("一次音频转写需要一个音频 Asset")
        asset = owned_input(blocks[0], context)
        body = {"model": model, "response_format": "json"}
        if "language" in upstream.profile:
            body["language"] = upstream.profile["language"]
        if prompt(request, context):
            body["prompt"] = prompt(request, context)
        with asset.path.open("rb") as source:
            raw = await self.transport.json(upstream, "/audio/transcriptions", headers, body=body, files=[
                ("file", (asset.descriptor.filename, source, asset.descriptor.mime_type)),
            ])
        if not isinstance(raw.get("text"), str) or not raw["text"].strip():
            raise upstream_error(code="UPSTREAM_INVALID_TRANSCRIPTION", retryable=False)
        usage = normalize_usage(raw.get("usage"), input_field="input_tokens", output_field="output_tokens")
        seconds = raw.get("duration")
        if not isinstance(seconds, (float, int)) or isinstance(seconds, bool) or seconds < 0:
            seconds = (raw.get("usage") or {}).get("seconds")
        if isinstance(seconds, (float, int)) and not isinstance(seconds, bool) and seconds >= 0:
            usage.media = MediaUsage(input_audio_seconds=seconds)
        usage.provider_raw = raw.get("usage") or {}
        return result([{"type": "text", "text": raw["text"]}], usage)


class OpenAISpeech(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="openai-speech", name="OpenAI-compatible Speech", operations=("llm",), streaming=True,
        tools=False, media={"input": ("text",), "output": ("audio",)}, probe_operation="speech_generation",
        endpoints={"llm": "/audio/speech", "discovery": "/models"},
        body_fields=("model", "input", "voice", "response_format", "speed", "instructions"),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "voice": {"type": "string", "minLength": 1},
            "speed": {"type": "number", "minimum": 0.25, "maximum": 4},
            "instructions": {"type": "string"},
        }},
        usage_semantics="binary speech has no token usage; duration is measured for WAV",
    )

    def body(self, request, context, model, upstream):
        if request.metadata.get("capability") != "speech_generation" or request.output.modalities != ["audio"]:
            raise unsupported("语音生成需要 speech_generation，输出模态为 audio")
        config = request.output.audio
        format = config.format if config else "mp3"
        if format not in {"mp3", "wav", "flac", "opus"}:
            raise unsupported("语音输出格式支持 mp3、wav、flac 和 opus")
        voice = config.voice if config and config.voice != "default" else upstream.profile.get("voice", "alloy")
        text = prompt(request, context)
        if not text:
            raise unsupported("语音生成需要文本")
        return {"model": model, "input": text, "voice": voice, "response_format": format,
                **{key: upstream.profile[key] for key in ("speed", "instructions") if key in upstream.profile}}

    async def _generate(self, request, context, model, upstream, headers, *, streaming):
        body = self.body(request, context, model, upstream)
        format = body["response_format"]
        mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac", "opus": "audio/ogg"}[format]
        with tempfile.TemporaryFile() as content:
            async with self.transport.response(upstream, "/audio/speech", headers, body=body) as response:
                size, first = 0, True
                try:
                    async for chunk in response.aiter_bytes(chunk_size=48 * 1024):
                        if first:
                            if detect_mime(chunk[:32]) != mime:
                                raise upstream_error(code="UPSTREAM_INVALID_AUDIO_CONTENT", retryable=False)
                            first = False
                        size += len(chunk)
                        if size > 100 * 1024 * 1024:
                            raise upstream_error(code="UPSTREAM_AUDIO_TOO_LARGE", retryable=False)
                        content.write(chunk)
                        if streaming:
                            yield Event(Kind.AUDIO_DELTA, item_id="msg_" + context.response_id, content_index=0,
                                        delta=base64.b64encode(chunk).decode(), data={"mime_type": mime})
                except httpx.HTTPError:
                    raise upstream_error(code="UPSTREAM_TRUNCATED_AUDIO", retryable=False) from None
            content.seek(0)
            duration = None
            if format == "wav":
                import wave
                try:
                    with wave.open(content, "rb") as source:
                        duration = source.getnframes() / source.getframerate()
                except (EOFError, ValueError, wave.Error):
                    raise upstream_error(code="UPSTREAM_INVALID_AUDIO_CONTENT", retryable=False) from None
            content.seek(0)
            async def chunks():
                while chunk := content.read(1024 * 1024):
                    yield chunk
            block = await store(context, "audio", filename="speech." + format, mime_type=mime, content=chunks())
            complete = result([block], Usage(media=MediaUsage(output_audio_seconds=duration)))
            complete.output[0]["id"] = "msg_" + context.response_id
            if streaming:
                yield Event(Kind.MEDIA_COMPLETED, item_id=complete.output[0]["id"], content_index=0,
                            item=complete.output[0])
            yield Event(Kind.COMPLETED, result=complete)

    async def execute(self, *args):
        async with aclosing(self._generate(*args, streaming=False)) as events:
            async for event in events:
                return event.result

    async def stream(self, *args):
        async with aclosing(self._generate(*args, streaming=True)) as events:
            async for event in events:
                yield event


class OpenAIAudioChat(DiscoverableAdapter):
    manifest = AdapterManifest(
        id="openai-audio-chat", name="OpenAI-compatible Audio Chat", operations=("llm",),
        tools=False, media={"input": ("text", "audio"), "output": ("text", "audio")},
        probe_operation="speech_to_speech",
        endpoints={"llm": "/chat/completions", "discovery": "/models"},
        body_fields=("model", "messages", "modalities", "audio", "stream"),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "voice": {"type": "string", "minLength": 1},
        }},
        usage_semantics="provider prompt/completion tokens, including audio; WAV output duration measured",
    )

    async def execute(self, request, context, model, upstream, headers):
        if request.metadata.get("capability") not in {"speech_generation", "speech_to_speech", "audio_transcription"}:
            raise unsupported("音频对话需要显式选择语音生成、语音转语音或音频转写")
        if not set(request.output.modalities) <= {"text", "audio"}:
            raise unsupported("音频对话仅支持文本和音频输出")
        config = request.output.audio
        format = config.format if config else "wav"
        if "audio" in request.output.modalities and format not in {"wav", "mp3", "flac", "opus"}:
            raise unsupported("音频对话输出格式支持 wav、mp3、flac 和 opus")
        body = {"model": model, "messages": openai_messages(request, context, media_inputs=True),
                "modalities": request.output.modalities, "stream": False}
        if "audio" in request.output.modalities:
            body["audio"] = {"format": format, "voice": config.voice if config and config.voice != "default"
                             else upstream.profile.get("voice", "alloy")}
        raw = await self.transport.json(upstream, "/chat/completions", headers, body=body)
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise upstream_error(code="UPSTREAM_INVALID_AUDIO_RESPONSE", retryable=False)
        message_raw = choices[0].get("message", {})
        blocks = []
        if "audio" in request.output.modalities:
            audio = message_raw.get("audio") or {}
            content = decoded(audio.get("data"))
            mime = detect_mime(content[:32])
            expected = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "opus": "audio/ogg"}[format]
            if mime != expected:
                raise upstream_error(code="UPSTREAM_INVALID_AUDIO_CONTENT", retryable=False)
            blocks.append(await store(context, "audio", filename="answer." + format, mime_type=mime, content=content))
        text = message_raw.get("content") or (message_raw.get("audio") or {}).get("transcript")
        if "text" in request.output.modalities and isinstance(text, str) and text:
            blocks.insert(0, {"type": "text", "text": text})
        if not blocks:
            raise upstream_error(code="UPSTREAM_INVALID_AUDIO_RESPONSE", retryable=False)
        usage = normalize_usage(raw.get("usage"), input_field="prompt_tokens", output_field="completion_tokens",
                                cache_field="prompt_tokens_details.cached_tokens")
        usage.provider_raw = raw.get("usage") or {}
        if "audio" in request.output.modalities:
            usage.media = MediaUsage(output_audio_seconds=audio_seconds(content))
        return result(blocks, usage, response_id=raw.get("id"))
