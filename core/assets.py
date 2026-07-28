"""租户隔离的 Kemo Asset 存储、校验与 Provider 访问边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.models import AssetDescriptor, ErrorObject
from core.provider_contract import AssetAccess, ResolvedAsset


_CHUNK_SIZE = 1024 * 1024
_CLEANUP_INTERVAL_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class AssetLimits:
    image_bytes: int = 20 * 1024 * 1024
    audio_bytes: int = 100 * 1024 * 1024
    video_bytes: int = 1024 * 1024 * 1024
    file_bytes: int = 100 * 1024 * 1024
    retention_hours: int = 24

    def for_mime(self, mime_type: str) -> int:
        category = mime_type.split("/", 1)[0].casefold()
        return {
            "image": self.image_bytes,
            "audio": self.audio_bytes,
            "video": self.video_bytes,
        }.get(category, self.file_bytes)


class AssetStoreFailure(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = ErrorObject(
            type="asset_error",
            code=code,
            message=message,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            details=details or {},
        )


class AssetStore:
    def __init__(self, root: Path, *, limits: AssetLimits | None = None) -> None:
        self.root = root.resolve()
        self.limits = limits or AssetLimits()
        self._lock_guard = asyncio.Lock()
        self._asset_locks: dict[str, asyncio.Lock] = {}
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await self.cleanup_expired()
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_stop.clear()
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(), name="kemo-asset-cleanup"
            )

    async def close(self) -> None:
        self._cleanup_stop.set()
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None:
            await task

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """删除过期内容但保留最小元数据，以便继续稳定返回 410。"""
        cutoff = now or datetime.now(timezone.utc)
        removed = 0
        for record_path in self.root.glob("*/*/metadata.json"):
            try:
                raw = json.loads(record_path.read_text(encoding="utf-8"))
                descriptor = AssetDescriptor.model_validate(raw.get("descriptor"))
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AttributeError):
                continue
            if descriptor.expires_at > cutoff:
                continue
            asset_id = record_path.parent.name
            lock = await self._asset_lock(asset_id)
            async with lock:
                content_path = record_path.parent / "content.bin"
                if content_path.is_file():
                    try:
                        content_path.unlink(missing_ok=True)
                    except OSError:
                        continue
                    removed += 1
        return removed

    async def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._cleanup_stop.wait(),
                    timeout=_CLEANUP_INTERVAL_SECONDS,
                )
            except TimeoutError:
                await self.cleanup_expired()

    def bind(self, tenant_id: str, subject_id: str) -> "BoundAssetAccess":
        return BoundAssetAccess(self, tenant_id=tenant_id, subject_id=subject_id)

    async def store_input(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
        metadata: Mapping[str, Any],
        idempotency_key: str,
        checksum_sha256: str | None,
    ) -> AssetDescriptor:
        return await self._store(
            tenant_id=tenant_id,
            subject_id=subject_id,
            purpose="input",
            filename=filename,
            mime_type=mime_type,
            chunks=chunks,
            metadata=metadata,
            idempotency_key=idempotency_key,
            checksum_sha256=checksum_sha256,
        )

    async def store_output(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        filename: str,
        mime_type: str,
        content: bytes | AsyncIterator[bytes],
        metadata: Mapping[str, Any] | None = None,
        checksum_sha256: str | None = None,
    ) -> AssetDescriptor:
        values = dict(metadata or {})
        stable = str(values.get("idempotency_key") or values.get("request_id") or "").strip()
        idempotency_key = (
            f"output:{stable}:{filename}" if stable else f"output:{uuid.uuid4().hex}"
        )

        async def byte_chunks() -> AsyncIterator[bytes]:
            if isinstance(content, bytes):
                if content:
                    yield content
                return
            async for chunk in content:
                yield chunk

        return await self._store(
            tenant_id=tenant_id,
            subject_id=subject_id,
            purpose="output",
            filename=filename,
            mime_type=mime_type,
            chunks=byte_chunks(),
            metadata=values,
            idempotency_key=idempotency_key,
            checksum_sha256=checksum_sha256,
        )

    def get(self, asset_id: str, *, tenant_id: str, subject_id: str) -> AssetDescriptor:
        descriptor, _ = self._load_owned(asset_id, tenant_id, subject_id)
        self._ensure_live(descriptor)
        return descriptor

    def resolve(self, asset_id: str, *, tenant_id: str, subject_id: str) -> ResolvedAsset:
        descriptor, content_path = self._load_owned(asset_id, tenant_id, subject_id)
        self._ensure_live(descriptor)
        if descriptor.status != "ready":
            raise AssetStoreFailure(
                409,
                "ASSET_NOT_READY",
                "Asset 尚未就绪。",
                retryable=True,
                retry_after_ms=1000,
                details={"asset_id": asset_id, "status": descriptor.status},
            )
        if not content_path.is_file():
            raise AssetStoreFailure(
                502,
                "ASSET_CONTENT_MISSING",
                "Asset 元数据存在，但内容文件不可用。",
                retryable=True,
                details={"asset_id": asset_id},
            )
        return ResolvedAsset(descriptor=descriptor, path=content_path)

    async def delete(self, asset_id: str, *, tenant_id: str, subject_id: str) -> None:
        lock = await self._asset_lock(asset_id)
        async with lock:
            descriptor, content_path = self._load_owned(asset_id, tenant_id, subject_id)
            if descriptor.status == "deleted":
                return
            try:
                content_path.unlink(missing_ok=True)
            except OSError as exc:
                raise AssetStoreFailure(
                    500,
                    "ASSET_DELETE_FAILED",
                    "Asset 内容删除失败。",
                    retryable=True,
                    details={"exception_type": type(exc).__name__},
                ) from exc
            updated = descriptor.model_copy(update={"status": "deleted"})
            self._write_record(
                asset_id,
                owner_hash=self._owner_hash(tenant_id, subject_id),
                idempotency_key="",
                descriptor=updated,
            )

    async def _store(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        purpose: str,
        filename: str,
        mime_type: str,
        chunks: AsyncIterator[bytes],
        metadata: Mapping[str, Any],
        idempotency_key: str,
        checksum_sha256: str | None,
    ) -> AssetDescriptor:
        normalized_key = idempotency_key.strip()
        if not normalized_key or len(normalized_key) > 256:
            raise AssetStoreFailure(
                400,
                "VALIDATION_ERROR",
                "Idempotency-Key 必须是 1-256 位非空字符串。",
            )
        declared_mime = self._normalize_mime(mime_type)
        safe_filename = self._safe_filename(filename)
        owner_hash = self._owner_hash(tenant_id, subject_id)
        asset_id = self._asset_id(owner_hash, normalized_key)
        asset_dir = self._asset_dir(asset_id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        temporary = asset_dir / f".{uuid.uuid4().hex}.upload"
        digest = hashlib.sha256()
        size = 0
        header = bytearray()
        declared_limit = self.limits.for_mime(declared_mime)
        try:
            with temporary.open("xb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AssetStoreFailure(
                            400, "INVALID_MEDIA", "Asset 数据块必须是 bytes。"
                        )
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > declared_limit:
                        raise AssetStoreFailure(
                            413,
                            "REQUEST_TOO_LARGE",
                            "Asset 超过当前媒体类型允许的大小。",
                            details={"limit_bytes": declared_limit},
                        )
                    if len(header) < 512:
                        header.extend(chunk[: 512 - len(header)])
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise AssetStoreFailure(400, "INVALID_MEDIA", "Asset 文件不能为空。")
            actual_checksum = digest.hexdigest()
            expected_checksum = self._normalize_checksum(checksum_sha256)
            if expected_checksum is not None and expected_checksum != actual_checksum:
                raise AssetStoreFailure(
                    400,
                    "INVALID_MEDIA",
                    "Asset SHA-256 与 X-Content-SHA256 不一致。",
                    details={"checksum_mismatch": True},
                )
            detected_mime = detect_mime(bytes(header))
            self._validate_mime(declared_mime, detected_mime)
            actual_limit = self.limits.for_mime(detected_mime or declared_mime)
            if size > actual_limit:
                raise AssetStoreFailure(
                    413,
                    "REQUEST_TOO_LARGE",
                    "Asset 超过检测后媒体类型允许的大小。",
                    details={"limit_bytes": actual_limit},
                )

            lock = await self._asset_lock(asset_id)
            async with lock:
                existing = self._try_load_owned(asset_id, owner_hash)
                if existing is not None:
                    descriptor, _ = existing
                    live = (
                        descriptor.status not in {"deleted", "failed"}
                        and descriptor.expires_at > datetime.now(timezone.utc)
                    )
                    if live and descriptor.checksum_sha256 != actual_checksum:
                        raise AssetStoreFailure(
                            409,
                            "IDEMPOTENCY_CONFLICT",
                            "相同 Idempotency-Key 对应不同 Asset 内容。",
                        )
                    if live:
                        return descriptor
                content_path = asset_dir / "content.bin"
                os.replace(temporary, content_path)
                now = datetime.now(timezone.utc)
                descriptor = AssetDescriptor(
                    id=asset_id,
                    status="ready",
                    purpose=purpose,
                    filename=safe_filename,
                    mime_type=detected_mime or declared_mime,
                    size=size,
                    checksum_sha256=actual_checksum,
                    created_at=now,
                    expires_at=now + timedelta(hours=self.limits.retention_hours),
                    metadata=dict(metadata),
                    extensions={},
                )
                self._write_record(
                    asset_id,
                    owner_hash=owner_hash,
                    idempotency_key=normalized_key,
                    descriptor=descriptor,
                )
                return descriptor
        finally:
            temporary.unlink(missing_ok=True)

    async def _asset_lock(self, asset_id: str) -> asyncio.Lock:
        async with self._lock_guard:
            return self._asset_locks.setdefault(asset_id, asyncio.Lock())

    def _load_owned(
        self, asset_id: str, tenant_id: str, subject_id: str
    ) -> tuple[AssetDescriptor, Path]:
        result = self._try_load_owned(asset_id, self._owner_hash(tenant_id, subject_id))
        if result is None:
            raise AssetStoreFailure(404, "ASSET_NOT_FOUND", "Asset 不存在或不可见。")
        return result

    def _try_load_owned(
        self, asset_id: str, owner_hash: str
    ) -> tuple[AssetDescriptor, Path] | None:
        if not asset_id.startswith("asset_") or len(asset_id) > 128:
            return None
        record_path = self._asset_dir(asset_id) / "metadata.json"
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("owner_hash") != owner_hash:
                return None
            descriptor = AssetDescriptor.model_validate(raw.get("descriptor"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None
        return descriptor, self._asset_dir(asset_id) / "content.bin"

    def _write_record(
        self,
        asset_id: str,
        *,
        owner_hash: str,
        idempotency_key: str,
        descriptor: AssetDescriptor,
    ) -> None:
        target = self._asset_dir(asset_id) / "metadata.json"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "owner_hash": owner_hash,
            "idempotency_key_hash": hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest(),
            "descriptor": descriptor.model_dump(mode="json"),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def _asset_dir(self, asset_id: str) -> Path:
        return self.root / asset_id[6:8] / asset_id

    @staticmethod
    def _owner_hash(tenant_id: str, subject_id: str) -> str:
        return hashlib.sha256(f"{tenant_id}\0{subject_id}".encode("utf-8")).hexdigest()

    @staticmethod
    def _asset_id(owner_hash: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(
            f"{owner_hash}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
        return f"asset_{digest[:40]}"

    @staticmethod
    def _safe_filename(filename: str) -> str:
        value = Path(filename or "asset.bin").name.strip().replace("\x00", "")
        return value[:255] or "asset.bin"

    @staticmethod
    def _normalize_mime(mime_type: str) -> str:
        value = mime_type.split(";", 1)[0].strip().casefold()
        if "/" not in value or len(value) > 127:
            raise AssetStoreFailure(400, "INVALID_MEDIA", "Asset MIME 类型无效。")
        return value

    @staticmethod
    def _normalize_checksum(value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().casefold()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise AssetStoreFailure(
                400,
                "VALIDATION_ERROR",
                "X-Content-SHA256 必须是 64 位十六进制 SHA-256。",
            )
        return normalized

    @staticmethod
    def _validate_mime(declared: str, detected: str | None) -> None:
        category = declared.split("/", 1)[0]
        if category in {"image", "audio", "video"} and detected is None:
            raise AssetStoreFailure(
                400,
                "INVALID_MEDIA",
                "无法从文件头确认声明的媒体类型。",
                details={"declared_mime_type": declared},
            )
        aliases = {"image/jpg": "image/jpeg", "audio/x-wav": "audio/wav"}
        normalized_declared = aliases.get(declared, declared)
        normalized_detected = aliases.get(detected or "", detected)
        if normalized_declared == "application/octet-stream" and detected is not None:
            return
        if detected is not None and normalized_declared != normalized_detected:
            raise AssetStoreFailure(
                400,
                "INVALID_MEDIA",
                "Asset MIME 与文件头不一致。",
                details={
                    "declared_mime_type": declared,
                    "detected_mime_type": detected,
                },
            )

    @staticmethod
    def _ensure_live(descriptor: AssetDescriptor) -> None:
        if descriptor.status == "deleted":
            raise AssetStoreFailure(410, "ASSET_DELETED", "Asset 已删除。")
        if descriptor.expires_at <= datetime.now(timezone.utc):
            raise AssetStoreFailure(410, "ASSET_EXPIRED", "Asset 已过期。")


class BoundAssetAccess(AssetAccess):
    def __init__(self, store: AssetStore, *, tenant_id: str, subject_id: str) -> None:
        self._store = store
        self._tenant_id = tenant_id
        self._subject_id = subject_id

    def resolve(self, asset_id: str) -> ResolvedAsset:
        return self._store.resolve(
            asset_id,
            tenant_id=self._tenant_id,
            subject_id=self._subject_id,
        )

    async def store_output(
        self,
        *,
        filename: str,
        mime_type: str,
        content: bytes | AsyncIterator[bytes],
        metadata: Mapping[str, Any] | None = None,
        checksum_sha256: str | None = None,
    ) -> AssetDescriptor:
        return await self._store.store_output(
            tenant_id=self._tenant_id,
            subject_id=self._subject_id,
            filename=filename,
            mime_type=mime_type,
            content=content,
            metadata=metadata,
            checksum_sha256=checksum_sha256,
        )


def detect_mime(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        major_brand = header[8:12]
        if major_brand in {b"M4A ", b"M4B ", b"mp4a"}:
            return "audio/mp4"
        return "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    return None


async def upload_chunks(upload: Any) -> AsyncIterator[bytes]:
    """从 FastAPI UploadFile 流式读取；保持存储层不依赖 Web 框架。"""
    while True:
        chunk = await upload.read(_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk
