"""Media results use the same owned Asset boundary as uploaded inputs."""
from __future__ import annotations

import base64
import io
import re
import wave

from core.assets import AssetStoreFailure
from core.models import Usage
from core.provider_contract import ProviderException, ProviderResult
from gateway.adapters.builtin.common import identifier, system_text, text_blocks, unsupported
from gateway.transport import upstream_error


def prompt(request, context):
    return "\n".join(filter(None, [system_text(request, context), *(
        text_blocks(item) for item in request.input if item.type == "message"
    )]))


def inputs(request, kind):
    return [block for item in request.input if item.type == "message"
            for block in item.content if block.type == kind]


def owned_input(block, context):
    identity = block.asset_id or (
        block.source.uri if block.source is not None and block.source.kind == "object_store" else None
    )
    if not identity or context.assets is None:
        raise unsupported("此媒体操作需要先上传 Asset，再使用 asset_id 引用")
    return context.assets.resolve(identity)


def native_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise upstream_error(code="UPSTREAM_INVALID_MEDIA_ID", retryable=False)
    return value


def decoded(value):
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise upstream_error(code="UPSTREAM_INVALID_MEDIA_BASE64", retryable=False) from None


def audio_seconds(content):
    if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(content)) as source:
                return source.getnframes() / source.getframerate()
        except (EOFError, ValueError, wave.Error):
            pass
    return None


async def store(context, kind, *, filename, mime_type, content, metadata=None):
    if context.assets is None:
        raise unsupported("媒体输出需要网关 Asset 存储")
    try:
        asset = await context.assets.store_output(
            filename=filename, mime_type=mime_type, content=content,
            metadata={"request_id": context.request_id, **(metadata or {})},
        )
    except AssetStoreFailure as exc:
        raise ProviderException(exc.error) from None
    return {"type": kind, "asset_id": asset.id, "mime_type": asset.mime_type,
            "checksum_sha256": asset.checksum_sha256,
            **({"filename": asset.filename} if kind == "file" else {})}


async def download(transport, upstream, path, headers, context, kind, *, filename, mime_type, metadata=None):
    # The caller supplies a path derived from validated upstream IDs. No URL
    # supplied by an upstream can redirect credentials or bypass Transport.
    try:
        async with transport.response(upstream, path, headers, method="GET") as response:
            return await store(context, kind, filename=filename, mime_type=mime_type,
                               content=response.aiter_bytes(), metadata=metadata)
    except ProviderException as exc:
        exc.error.retryable = False  # Generation already happened; do not submit it again.
        raise


def result(blocks, usage=None, *, response_id=None):
    return ProviderResult("completed", [{
        "type": "message", "id": identifier("msg"), "role": "assistant",
        "phase": "final_answer", "content": blocks,
    }], usage or Usage(), response_id)
