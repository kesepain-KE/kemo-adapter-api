"""Shared Kemo protocol-version header validation for public endpoints."""

from __future__ import annotations

from fastapi import HTTPException


CURRENT_PROTOCOL_VERSION = "2.0"
SUPPORTED_PROTOCOL_VERSIONS = (CURRENT_PROTOCOL_VERSION,)


def validate_protocol_version(
    header_version: str | None,
    *,
    body_version: str | None = None,
) -> str:
    """Validate the required transport header and optional JSON body version."""

    if header_version is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "缺少 X-Kemo-Protocol-Version",
            },
        )
    if body_version is not None and header_version != body_version:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PROTOCOL_ERROR",
                "message": "header/body 协议版本不一致",
            },
        )
    if header_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "UNSUPPORTED_PROTOCOL_VERSION",
                "message": "协议版本不受支持",
                "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            },
        )
    return header_version


__all__ = [
    "CURRENT_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "validate_protocol_version",
]
