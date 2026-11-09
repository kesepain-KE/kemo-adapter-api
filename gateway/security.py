from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from Crypto.Cipher import AES


class SecretVault:
    """AES-GCM credentials; master material is outside the catalog database."""

    def __init__(self, data_root: Path):
        raw = os.environ.get("KEMO_MASTER_KEY", "")
        self._key_path = data_root / "master.key"
        self._key: bytes | None = None
        if raw:
            try:
                self._key = base64.b64decode(raw, validate=True)
            except ValueError:
                raise ValueError("KEMO_MASTER_KEY 必须是 32 字节主密钥的 Base64") from None
            if len(self._key) != 32:
                raise ValueError("KEMO_MASTER_KEY 必须是 32 字节主密钥的 Base64")
        elif self._key_path.exists():
            value = self._key_path.read_bytes()
            if os.name == "nt":
                value = self._windows_protect(value, decrypt=True)
            self._key = value
            if len(value) != 32:
                raise ValueError("本地主密钥损坏")

    @staticmethod
    def _windows_protect(value: bytes, *, decrypt: bool = False) -> bytes:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

        source_buffer = ctypes.create_string_buffer(value)
        source = Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
        target = Blob()
        function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise OSError("Windows DPAPI 主密钥操作失败")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)

    def _material(self) -> bytes:
        if self._key is None:
            if os.name != "nt":
                raise ValueError("请先配置 KEMO_MASTER_KEY，才能保存上游或创建下游密钥")
            self._key = secrets.token_bytes(32)
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            with self._key_path.open("xb") as f:
                f.write(self._windows_protect(self._key))
        return self._key

    def encrypt(self, value: str, identity: str) -> str:
        cipher = AES.new(self._material(), AES.MODE_GCM)
        cipher.update(identity.encode())
        encrypted, tag = cipher.encrypt_and_digest(value.encode())
        return base64.b64encode(cipher.nonce + tag + encrypted).decode()

    def decrypt(self, value: str, identity: str) -> str:
        raw = base64.b64decode(value, validate=True)
        cipher = AES.new(self._material(), AES.MODE_GCM, nonce=raw[:16])
        cipher.update(identity.encode())
        return cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode()

    def fingerprint(self, token: str) -> str:
        pepper = hmac.digest(self._material(), b"kemo-downstream-pepper-v1", "sha256")
        return hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()


def preview(value: str) -> str:
    return f"{value[:10]}…{value[-5:]}" if len(value) > 15 else "***"
