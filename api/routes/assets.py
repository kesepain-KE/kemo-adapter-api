"""Kemo 多模态 Asset 上传、查询、读取和删除接口。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from api.middleware import Principal, authenticated_principal
from core.assets import AssetStore, upload_chunks
from core.models import AssetDescriptor


router = APIRouter(prefix="/assets", tags=["assets"])


def _asset_store(request: Request) -> AssetStore:
    return request.app.state.assets


def _require_scope(principal: Principal, scope: str) -> None:
    if "owner" not in principal.scopes and scope not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTHORIZATION_ERROR", "message": f"当前密钥缺少 {scope} 权限"},
        )


@router.post("", response_model=AssetDescriptor, status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    metadata: str = Form("{}"),
    principal: Principal = Depends(authenticated_principal),
    store: AssetStore = Depends(_asset_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    checksum_sha256: str | None = Header(default=None, alias="X-Content-SHA256"),
    protocol_version: str | None = Header(default=None, alias="X-Kemo-Protocol-Version"),
) -> AssetDescriptor:
    _require_scope(principal, "asset:write")
    if protocol_version != "1.0":
        raise HTTPException(
            status_code=400,
            detail={"code": "PROTOCOL_VERSION_ERROR", "message": "X-Kemo-Protocol-Version 必须为 1.0"},
        )
    if idempotency_key is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "缺少 Idempotency-Key"},
        )
    if len(metadata.encode("utf-8")) > 64 * 1024:
        raise HTTPException(
            status_code=413,
            detail={"code": "REQUEST_TOO_LARGE", "message": "Asset metadata 超过 64 KiB"},
        )
    try:
        parsed_metadata = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "Asset metadata 不是有效 JSON"},
        ) from exc
    if not isinstance(parsed_metadata, dict):
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "Asset metadata 根节点必须是对象"},
        )
    if parsed_metadata.get("purpose", "input") != "input":
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "公开上传只接受 purpose=input"},
        )
    return await store.store_input(
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
        filename=file.filename or "asset.bin",
        mime_type=file.content_type or "application/octet-stream",
        chunks=upload_chunks(file),
        metadata=parsed_metadata,
        idempotency_key=idempotency_key,
        checksum_sha256=checksum_sha256,
    )


@router.get("/{asset_id}", response_model=AssetDescriptor)
async def get_asset(
    asset_id: str,
    response: Response,
    principal: Principal = Depends(authenticated_principal),
    store: AssetStore = Depends(_asset_store),
) -> AssetDescriptor:
    _require_scope(principal, "asset:read")
    descriptor = store.get(
        asset_id,
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
    )
    if descriptor.status in {"uploading", "processing"}:
        response.status_code = 202
    return descriptor


@router.get("/{asset_id}/content")
async def get_asset_content(
    asset_id: str,
    principal: Principal = Depends(authenticated_principal),
    store: AssetStore = Depends(_asset_store),
    range_header: str | None = Header(default=None, alias="Range"),
) -> StreamingResponse:
    _require_scope(principal, "asset:read")
    resolved = store.resolve(
        asset_id,
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
    )
    start, end, partial = _resolve_range(range_header, resolved.descriptor.size)
    length = end - start + 1

    def content() -> Iterator[bytes]:
        with resolved.path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "ETag": f'"{resolved.descriptor.checksum_sha256}"',
        "Content-Disposition": _content_disposition(resolved.descriptor.filename),
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{resolved.descriptor.size}"
    return StreamingResponse(
        content(),
        status_code=206 if partial else 200,
        media_type=resolved.descriptor.mime_type,
        headers=headers,
    )


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: str,
    principal: Principal = Depends(authenticated_principal),
    store: AssetStore = Depends(_asset_store),
) -> Response:
    _require_scope(principal, "asset:write")
    await store.delete(
        asset_id,
        tenant_id=principal.tenant_id,
        subject_id=principal.subject_id,
    )
    return Response(status_code=204)


def _resolve_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(
            status_code=416,
            detail={"code": "INVALID_RANGE", "message": "只支持单段 bytes Range"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    spec = value[6:].strip()
    start_raw, separator, end_raw = spec.partition("-")
    if not separator:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "Range 格式无效"})
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
    except ValueError as exc:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "Range 格式无效"}) from exc
    if start < 0 or start >= size or end < start:
        raise HTTPException(
            status_code=416,
            detail={"code": "INVALID_RANGE", "message": "Range 超出 Asset 范围"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    return start, min(end, size - 1), True


def _content_disposition(filename: str) -> str:
    safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    fallback = safe.encode("ascii", "ignore").decode("ascii").strip() or "asset.bin"
    encoded = quote(safe, safe="")
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
