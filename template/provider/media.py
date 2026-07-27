"""Kemo 媒体内容块解析器；复制 Provider 后按厂商真实来源能力选择转换。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


SUPPORTED_SOURCE_KINDS = frozenset(
    {"url", "data_url", "object_store", "inline_base64", "provider_file_id"}
)


class MediaSourceError(ValueError):
    """可由 protocol.py 转换为脱敏 VALIDATION_ERROR 的媒体结构错误。"""

    def __init__(
        self,
        message: str,
        *,
        location: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.location = location
        self.details = {"location": location, **(details or {})}


@dataclass(frozen=True, slots=True)
class ParsedMediaSource:
    media_type: str
    mime_type: str | None
    kind: str
    uri: str | None = None
    data: str | None = None
    provider: str | None = None
    file_id: str | None = None
    detail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_data_url(self) -> str:
        """只转换 Kemo 已提供的 Data URL 或内联 Base64，不下载远程资源。"""
        if self.kind == "data_url" and self.uri:
            return self.uri
        if self.kind == "inline_base64" and self.data and self.mime_type:
            return f"data:{self.mime_type};base64,{self.data}"
        raise MediaSourceError(
            "该媒体来源不能直接转换为 Data URL",
            location="source",
            details={"source_kind": self.kind},
        )


def parse_media_block(
    block: Mapping[str, Any],
    *,
    expected_type: str,
    location: str,
) -> ParsedMediaSource:
    """解析真实 Kemo 媒体块，但不替厂商决定其支持哪些来源。"""
    media_type = str(block.get("type") or "").strip()
    if media_type != expected_type:
        raise MediaSourceError(
            f"媒体类型必须是 {expected_type}",
            location=location,
            details={"content_type": media_type or None},
        )

    source = block.get("source")
    if not isinstance(source, Mapping):
        raise MediaSourceError(
            "媒体 source 必须是对象",
            location=f"{location}.source",
        )

    kind = str(source.get("kind") or "").strip()
    if kind not in SUPPORTED_SOURCE_KINDS:
        raise MediaSourceError(
            "不支持的 Kemo 媒体来源类型",
            location=f"{location}.source.kind",
            details={
                "source_kind": kind or None,
                "supported_source_kinds": sorted(SUPPORTED_SOURCE_KINDS),
            },
        )

    mime_type = str(block.get("mime_type") or "").strip() or None
    if mime_type is not None and not mime_type.startswith(f"{expected_type}/"):
        raise MediaSourceError(
            "媒体 MIME 与内容类型不一致",
            location=f"{location}.mime_type",
            details={"content_type": expected_type, "mime_type": mime_type},
        )

    uri: str | None = None
    data: str | None = None
    provider: str | None = None
    file_id: str | None = None
    if kind in {"url", "data_url", "object_store"}:
        uri = str(source.get("uri") or "").strip() or None
        if uri is None:
            raise MediaSourceError(
                "该媒体来源要求非空 source.uri",
                location=f"{location}.source.uri",
                details={"source_kind": kind},
            )
        if kind == "data_url" and not uri.startswith(f"data:{expected_type}/"):
            raise MediaSourceError(
                "Data URL 类型与媒体内容不一致",
                location=f"{location}.source.uri",
                details={"source_kind": kind, "content_type": expected_type},
            )
    elif kind == "inline_base64":
        data = str(source.get("data") or "").strip() or None
        if data is None:
            raise MediaSourceError(
                "内联媒体要求非空 source.data",
                location=f"{location}.source.data",
                details={"source_kind": kind},
            )
        if mime_type is None:
            raise MediaSourceError(
                "内联媒体必须提供内容块级 mime_type",
                location=f"{location}.mime_type",
                details={"source_kind": kind},
            )
    else:
        provider = str(source.get("provider") or "").strip() or None
        file_id = str(source.get("file_id") or "").strip() or None
        if provider is None or file_id is None:
            raise MediaSourceError(
                "provider_file_id 要求 source.provider 和 source.file_id",
                location=f"{location}.source",
                details={"source_kind": kind},
            )

    detail_value = block.get("detail")
    detail = str(detail_value) if detail_value in {"auto", "low", "high"} else None
    metadata = block.get("metadata")
    return ParsedMediaSource(
        media_type=media_type,
        mime_type=mime_type,
        kind=kind,
        uri=uri,
        data=data,
        provider=provider,
        file_id=file_id,
        detail=detail,
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )
