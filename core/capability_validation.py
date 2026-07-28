"""在请求进入 Provider 前校验其公开能力声明。

核心只检查统一 Kemo 能力，不理解任何厂商字段或端点。Provider 仍负责厂商协议转换和更细的
参数校验。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from collections.abc import Mapping
from typing import Any, Never
from urllib.parse import urlparse

from core.assets import detect_mime
from core.models import ErrorObject, KemoRequest, ModelCapabilities
from core.provider_contract import AssetAccess, ProviderException


_TEXT_CONTENT_TYPES = frozenset({"text", "json"})
_KNOWN_CONTENT_MODALITIES = frozenset(
    {"image", "audio", "video", "file"}
)
_MULTIMODAL_CONTENT_TYPES = frozenset({"image", "audio", "video", "file"})
_OPERATIONS = {
    "conversation": (set(), {"text"}),
    "vision": ({"text", "image"}, {"text"}),
    "image_generation": ({"text"}, {"image"}),
    "image_edit": ({"text", "image"}, {"image"}),
    "audio_transcription": ({"audio"}, {"text"}),
    "speech_generation": ({"text"}, {"audio"}),
    "speech_to_speech": ({"audio"}, {"audio"}),
    "video_understanding": ({"video"}, {"text"}),
    "video_generation": ({"text"}, {"video"}),
}
_ASSET_ROLES = frozenset(
    {"source", "mask", "reference", "style", "first_frame", "last_frame"}
)
_BLOCKED_MEDIA_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)


def validate_llm_request_capabilities(
    request: KemoRequest,
    capabilities: ModelCapabilities,
    *,
    asset_access: AssetAccess | None = None,
) -> None:
    """依据 Provider 的声明验证统一 LLM 请求，失败时返回稳定脱敏错误。"""
    if capabilities.model != request.model:
        _fail(
            "CAPABILITIES_MODEL_MISMATCH",
            "Provider 返回的能力声明与请求模型不一致。",
            details={
                "request_model": request.model,
                "capabilities_model": capabilities.model,
            },
        )
    if capabilities.task != "llm":
        _fail(
            "MODEL_TASK_MISMATCH",
            "该模型不是 LLM，不能通过 /model/responses 调用。",
            details={"model": request.model, "task": capabilities.task},
        )

    requested_inputs = _request_input_modalities(request.input)
    unsupported_inputs = requested_inputs - set(capabilities.input_modalities)
    if unsupported_inputs:
        _fail(
            "UNSUPPORTED_INPUT_MODALITY",
            f"模型不支持输入模态: {sorted(unsupported_inputs)}",
            details={
                "model": request.model,
                "requested_modalities": sorted(requested_inputs),
                "supported_modalities": sorted(capabilities.input_modalities),
            },
        )

    requested_outputs = _request_output_modalities(request.output)
    unsupported_outputs = requested_outputs - set(capabilities.output_modalities)
    if unsupported_outputs:
        _fail(
            "UNSUPPORTED_OUTPUT_MODALITY",
            f"模型不支持输出模态: {sorted(unsupported_outputs)}",
            details={
                "model": request.model,
                "requested_modalities": sorted(requested_outputs),
                "supported_modalities": sorted(capabilities.output_modalities),
            },
        )

    _validate_operation(
        request,
        capabilities,
        requested_inputs=requested_inputs,
        requested_outputs=requested_outputs,
    )
    _validate_multimodal_tool_results(request, capabilities)
    _validate_multimodal_metadata(request)
    _validate_media_sources(request, asset_access)

    if request.stream and not capabilities.streaming:
        _fail(
            "STREAMING_UNSUPPORTED",
            "该模型未声明支持流式响应。",
            details={"model": request.model},
        )
    if request.tools and not capabilities.tools.function_calling:
        _fail(
            "TOOLS_UNSUPPORTED",
            "该模型未声明支持工具调用。",
            details={"model": request.model},
        )
    if (
        request.tools
        and bool((request.generation or {}).get("parallel_tool_calls", True))
        and not capabilities.tools.parallel_calls
    ):
        _fail(
            "PARALLEL_TOOLS_UNSUPPORTED",
            "该模型未声明支持并行工具调用。",
            details={"model": request.model},
        )

    reasoning = request.reasoning or {}
    if bool(reasoning.get("enabled", False)):
        if not capabilities.reasoning.supported:
            _fail(
                "REASONING_UNSUPPORTED",
                "该模型未声明支持推理模式。",
                details={"model": request.model},
            )
        effort = str(reasoning.get("effort") or "").strip().lower()
        supported_efforts = set(capabilities.reasoning.efforts)
        if effort and effort != "none" and effort not in supported_efforts:
            _fail(
                "REASONING_EFFORT_UNSUPPORTED",
                f"模型不支持推理档位: {effort}",
                details={
                    "model": request.model,
                    "effort": effort,
                    "supported_efforts": sorted(supported_efforts),
                },
        )


async def validate_media_url_networks(request: KemoRequest) -> None:
    """在 Provider 执行前对外部媒体主机执行 DNS/IP 双重 SSRF 检查。"""
    hosts: set[tuple[str, int]] = set()
    for item in request.input:
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            source = block.get("source")
            if not isinstance(source, Mapping) or source.get("kind") != "url":
                continue
            parsed = urlparse(str(source.get("uri") or ""))
            if parsed.hostname:
                hosts.add((parsed.hostname.rstrip(".").casefold(), parsed.port or 443))

    for hostname, port in hosts:
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    hostname,
                    port,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                ),
                timeout=2.0,
            )
        except (OSError, TimeoutError) as exc:
            raise ProviderException(
                ErrorObject(
                    type="media_source_error",
                    code="MEDIA_SOURCE_UNREACHABLE",
                    message="外部媒体主机暂时无法安全解析。",
                    retryable=True,
                    details={"exception_type": type(exc).__name__},
                )
            ) from exc
        if not addresses:
            _fail(
                "INVALID_MEDIA",
                "外部媒体主机没有可用地址。",
                details={"source_kind": "url"},
            )
        for result in addresses:
            address = ipaddress.ip_address(result[4][0])
            if not address.is_global:
                _fail(
                    "INVALID_MEDIA",
                    "外部媒体主机解析到非公网地址。",
                    details={"source_kind": "url"},
                )


def _request_input_modalities(items: list[dict[str, Any]]) -> set[str]:
    modalities: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type in _TEXT_CONTENT_TYPES:
                modalities.add("text")
            elif block_type in _KNOWN_CONTENT_MODALITIES:
                modalities.add(block_type)
    return modalities


def _request_output_modalities(output: Mapping[str, Any]) -> set[str]:
    raw = output.get("modalities")
    if not isinstance(raw, list) or not raw:
        return {"text"}
    return {str(value).strip() for value in raw if str(value).strip()}


def _validate_operation(
    request: KemoRequest,
    capabilities: ModelCapabilities,
    *,
    requested_inputs: set[str],
    requested_outputs: set[str],
) -> None:
    capability = str(request.metadata.get("capability") or "conversation").strip()
    requirements = _OPERATIONS.get(capability)
    if requirements is None:
        _fail(
            "UNKNOWN_MULTIMODAL_OPERATION",
            f"未知的 metadata.capability: {capability}",
            details={"capability": capability, "supported": sorted(_OPERATIONS)},
        )

    operations = capabilities.extensions.get("operations")
    declaration = operations.get(capability) if isinstance(operations, Mapping) else None
    if capability != "conversation" and not (
        declaration is True
        or isinstance(declaration, Mapping) and declaration.get("supported") is True
    ):
        _fail(
            "MULTIMODAL_OPERATION_UNSUPPORTED",
            f"模型未声明支持多模态操作: {capability}",
            details={"model": request.model, "capability": capability},
        )
    if declaration is False or (
        isinstance(declaration, Mapping) and declaration.get("supported") is False
    ):
        _fail(
            "MULTIMODAL_OPERATION_UNSUPPORTED",
            f"模型明确声明不支持操作: {capability}",
            details={"model": request.model, "capability": capability},
        )

    required_inputs, required_outputs = requirements
    missing_inputs = required_inputs - requested_inputs
    missing_outputs = required_outputs - requested_outputs
    if missing_inputs or missing_outputs:
        _fail(
            "MULTIMODAL_OPERATION_MISMATCH",
            f"请求内容与操作 {capability} 不匹配。",
            details={
                "capability": capability,
                "missing_input_modalities": sorted(missing_inputs),
                "missing_output_modalities": sorted(missing_outputs),
            },
        )
    if capability != "conversation" and request.tools:
        _fail(
            "MULTIMODAL_TOOLS_FORBIDDEN",
            "专用多模态操作不得携带业务工具。",
            details={"capability": capability},
        )


def _validate_multimodal_tool_results(
    request: KemoRequest,
    capabilities: ModelCapabilities,
) -> None:
    for item_index, item in enumerate(request.input):
        if item.get("type") != "tool_result":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        media_types = sorted(
            {
                str(block.get("type"))
                for block in content
                if isinstance(block, Mapping)
                and block.get("type") in _MULTIMODAL_CONTENT_TYPES
            }
        )
        if media_types and not capabilities.tools.multimodal_results:
            _fail(
                "MULTIMODAL_TOOL_RESULTS_UNSUPPORTED",
                "模型未声明支持多模态工具结果。",
                details={
                    "model": request.model,
                    "input_index": item_index,
                    "media_types": media_types,
                },
            )


def _validate_multimodal_metadata(request: KemoRequest) -> None:
    multimodal = request.metadata.get("multimodal")
    if multimodal is None:
        return
    if not isinstance(multimodal, Mapping):
        _fail(
            "INVALID_MULTIMODAL_METADATA",
            "metadata.multimodal 必须是对象。",
            details={},
        )
    assets = multimodal.get("assets", [])
    if not isinstance(assets, list):
        _fail(
            "INVALID_MULTIMODAL_METADATA",
            "metadata.multimodal.assets 必须是数组。",
            details={},
        )
    for index, asset in enumerate(assets):
        if not isinstance(asset, Mapping):
            _fail(
                "INVALID_MULTIMODAL_METADATA",
                "多模态 Asset 说明必须是对象。",
                details={"asset_index": index},
            )
        asset_id = str(asset.get("asset_id") or "").strip()
        role = str(asset.get("role") or "").strip()
        if not asset_id or role not in _ASSET_ROLES:
            _fail(
                "INVALID_MULTIMODAL_ASSET_ROLE",
                "多模态 Asset 必须包含合法 asset_id 和 role。",
                details={
                    "asset_index": index,
                    "asset_id": asset_id or None,
                    "role": role or None,
                    "supported_roles": sorted(_ASSET_ROLES),
                },
            )


def _validate_media_sources(
    request: KemoRequest,
    asset_access: AssetAccess | None,
) -> None:
    referenced_assets: set[str] = set()
    for item_index, item in enumerate(request.input):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "")
            if block_type not in _MULTIMODAL_CONTENT_TYPES:
                continue
            location = f"input[{item_index}].content[{block_index}]"
            mime_type = str(block.get("mime_type") or "").strip().casefold()
            if (
                block_type != "file"
                and mime_type
                and not mime_type.startswith(f"{block_type}/")
            ):
                _fail(
                    "INVALID_MEDIA",
                    "媒体 MIME 与内容类型不一致。",
                    details={"location": location, "mime_type": mime_type},
                )
            asset_id = str(block.get("asset_id") or "").strip()
            if asset_id:
                if asset_access is None:
                    _fail(
                        "ASSET_API_UNAVAILABLE",
                        "当前执行上下文没有可用的 Asset 解析器。",
                        details={"location": location, "asset_id": asset_id},
                    )
                resolved = asset_access.resolve(asset_id)
                descriptor = resolved.descriptor
                referenced_assets.add(asset_id)
                if (
                    block_type != "file"
                    and not descriptor.mime_type.startswith(f"{block_type}/")
                ):
                    _fail(
                        "INVALID_MEDIA",
                        "Asset 类型与内容块类型不一致。",
                        details={
                            "location": location,
                            "asset_id": asset_id,
                            "asset_mime_type": descriptor.mime_type,
                        },
                    )
                if mime_type and mime_type != descriptor.mime_type:
                    _fail(
                        "INVALID_MEDIA",
                        "内容块 MIME 与 Asset MIME 不一致。",
                        details={"location": location, "asset_id": asset_id},
                    )
                checksum = str(block.get("checksum_sha256") or "").strip().casefold()
                if checksum and checksum != descriptor.checksum_sha256:
                    _fail(
                        "INVALID_MEDIA",
                        "内容块 SHA-256 与 Asset 不一致。",
                        details={"location": location, "asset_id": asset_id},
                    )
            source = block.get("source")
            if isinstance(source, Mapping):
                _validate_inline_source(
                    source,
                    block_type,
                    mime_type=mime_type or None,
                    location=location,
                )

    multimodal = request.metadata.get("multimodal")
    declared_assets = (
        multimodal.get("assets", []) if isinstance(multimodal, Mapping) else []
    )
    for index, asset in enumerate(declared_assets):
        if not isinstance(asset, Mapping):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        if asset_id not in referenced_assets:
            _fail(
                "MULTIMODAL_ASSET_NOT_REFERENCED",
                "metadata.multimodal 中的 Asset 必须同时出现在 input content 中。",
                details={"asset_index": index, "asset_id": asset_id},
            )


def _validate_inline_source(
    source: Mapping[str, Any],
    block_type: str,
    *,
    mime_type: str | None,
    location: str,
) -> None:
    kind = str(source.get("kind") or "")
    if kind == "url":
        uri = str(source.get("uri") or "")
        parsed = urlparse(uri)
        if parsed.scheme != "https" or not parsed.hostname:
            _fail(
                "INVALID_MEDIA",
                "外部媒体 URL 必须使用 HTTPS。",
                details={"location": location, "source_kind": kind},
            )
        try:
            port = parsed.port
        except ValueError:
            port = None
            _fail(
                "INVALID_MEDIA",
                "外部媒体 URL 端口无效。",
                details={"location": location, "source_kind": kind},
            )
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            _fail(
                "INVALID_MEDIA",
                "外部媒体 URL 不得包含凭据或 fragment。",
                details={"location": location, "source_kind": kind},
            )
        if port is not None and port not in {443, 8443}:
            _fail(
                "INVALID_MEDIA",
                "外部媒体 URL 只允许受控 HTTPS 端口。",
                details={"location": location, "source_kind": kind},
            )
        hostname = parsed.hostname.rstrip(".").casefold()
        if (
            hostname in _BLOCKED_MEDIA_HOSTS
            or hostname.endswith(".local")
            or hostname.endswith(".internal")
        ):
            _fail(
                "INVALID_MEDIA",
                "外部媒体 URL 不得指向本地地址。",
                details={"location": location, "source_kind": kind},
            )
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            _fail(
                "INVALID_MEDIA",
                "外部媒体 URL 不得指向非公网 IP。",
                details={"location": location, "source_kind": kind},
            )
    elif kind == "data_url":
        value = str(source.get("uri") or "")
        header, separator, encoded = value.partition(",")
        source_mime = header[5:].split(";", 1)[0].strip().casefold()
        if (
            not separator
            or not header.startswith("data:")
            or not header.casefold().endswith(";base64")
            or not source_mime
            or (block_type != "file" and not source_mime.startswith(f"{block_type}/"))
            or (mime_type is not None and source_mime != mime_type)
        ):
            _fail(
                "INVALID_MEDIA",
                "Data URL 类型或编码格式无效。",
                details={"location": location, "source_kind": kind},
            )
        decoded = _validate_base64_payload(encoded, location=location)
        _validate_inline_mime(
            decoded,
            block_type=block_type,
            mime_type=source_mime,
            location=location,
        )
    elif kind == "inline_base64":
        decoded = _validate_base64_payload(
            str(source.get("data") or ""), location=location
        )
        _validate_inline_mime(
            decoded,
            block_type=block_type,
            mime_type=mime_type,
            location=location,
        )


def _validate_base64_payload(value: str, *, location: str) -> bytes:
    if len(value) > 1_398_104:
        _fail(
            "REQUEST_TOO_LARGE",
            "内联媒体解码后不得超过 1 MiB。",
            details={"location": location, "limit_bytes": 1024 * 1024},
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        _fail(
            "INVALID_MEDIA",
            "媒体 Base64 无效。",
            details={"location": location, "exception_type": type(exc).__name__},
        )
    if not decoded or len(decoded) > 1024 * 1024:
        _fail(
            "REQUEST_TOO_LARGE" if decoded else "INVALID_MEDIA",
            "内联媒体为空或超过 1 MiB。",
            details={"location": location, "limit_bytes": 1024 * 1024},
        )
    return decoded


def _validate_inline_mime(
    decoded: bytes,
    *,
    block_type: str,
    mime_type: str | None,
    location: str,
) -> None:
    detected = detect_mime(decoded[:512])
    if block_type in {"image", "audio", "video"} and detected is None:
        _fail(
            "INVALID_MEDIA",
            "无法从内联媒体文件头确认媒体类型。",
            details={"location": location, "block_type": block_type},
        )
    if detected is not None and (
        (block_type != "file" and not detected.startswith(f"{block_type}/"))
        or (mime_type is not None and detected != mime_type)
    ):
        _fail(
            "INVALID_MEDIA",
            "内联媒体 MIME 与文件头不一致。",
            details={"location": location, "block_type": block_type},
        )


def _fail(code: str, message: str, *, details: dict[str, Any]) -> Never:
    raise ProviderException(
        ErrorObject(
            type="capability_validation",
            code=code,
            message=message,
            retryable=False,
            details=details,
        )
    )
