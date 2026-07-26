"""Read-only restart-change and release-version inspection for the Web console."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/kesepain-KE/"
    "kemo-adapter-api/main/version.json"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _excluded(relative: str) -> bool:
    parts = relative.split("/")
    if "__pycache__" in parts or relative.endswith((".pyc", ".pyo")):
        return True
    if relative in {
        "api/keys.json",
        "api/runtime.json",
        "core/live_control.json",
    }:
        return True
    if relative.startswith(("core/runtime/", "storage/")):
        return True
    if (
        len(parts) >= 3
        and parts[0] == "providers"
        and parts[-1] in {"config.json", "secrets.json"}
    ):
        return True
    return False


class SystemInspector:
    """Keep a process-start snapshot without exposing file contents to clients."""

    _GROUP_PATTERNS: dict[str, tuple[str, ...]] = {
        "environment": (".env",),
        "startup": ("start_web.py", "restart.py"),
        "backend": ("api/**/*.py", "core/**/*.py", "web/backend/**/*.py"),
        "frontend": ("web/frontend/dist/**/*",),
        "providers": ("providers/**/*",),
        "dependencies": (
            "setup.py",
            "pyproject.toml",
            "requirements*.txt",
            "poetry.lock",
            "uv.lock",
            "web/frontend/package.json",
            "web/frontend/pnpm-lock.yaml",
        ),
        "version": ("version.json",),
    }

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._startup_fingerprints = self._fingerprints()
        self._version_cache: dict[str, Any] | None = None
        self._version_cache_until = 0.0
        self._version_cache_lock = threading.Lock()

    def _group_files(self, patterns: tuple[str, ...]) -> list[Path]:
        files: set[Path] = set()
        for pattern in patterns:
            files.update(path for path in self.project_root.glob(pattern) if path.is_file())
        return sorted(
            (
                path
                for path in files
                if not _excluded(path.relative_to(self.project_root).as_posix())
            ),
            key=lambda path: path.relative_to(self.project_root).as_posix(),
        )

    def _fingerprint_group(self, patterns: tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        for path in self._group_files(patterns):
            relative = path.relative_to(self.project_root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            try:
                with path.open("rb") as handle:
                    while chunk := handle.read(128 * 1024):
                        digest.update(chunk)
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        return digest.hexdigest()

    def _fingerprints(self) -> dict[str, str]:
        return {
            group: self._fingerprint_group(patterns)
            for group, patterns in self._GROUP_PATTERNS.items()
        }

    def restart_required(self) -> dict[str, Any]:
        current = self._fingerprints()
        changed = [
            group
            for group in self._GROUP_PATTERNS
            if current[group] != self._startup_fingerprints[group]
        ]
        required = bool(changed)
        return {
            "required": required,
            "message": (
                "检测到需要重启的变量，请重启"
                if required
                else "当前没有检测到需要重启的改动"
            ),
            "changed_groups": changed,
            "checked_at": _utc_now(),
        }

    @staticmethod
    def _read_version_payload(raw: bytes) -> dict[str, Any]:
        if len(raw) > 64 * 1024:
            raise ValueError("version payload is too large")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("version"), str):
            raise ValueError("version payload is invalid")
        return {
            "version": value["version"],
            "protocol_version": value.get("protocol_version"),
            "build": value.get("build"),
            "notes": value.get("notes"),
        }

    @staticmethod
    def _version_key(value: str) -> tuple[tuple[int, ...], int, str]:
        match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.-]+))?", value.strip())
        if match is None:
            raise ValueError("invalid version")
        normalized = [int(part) for part in match.group(1).split(".")]
        while len(normalized) > 1 and normalized[-1] == 0:
            normalized.pop()
        numbers = tuple(normalized)
        suffix = match.group(2)
        # A stable version is newer than a prerelease with the same number.
        return numbers, 1 if suffix is None else 0, suffix or ""

    def _local_version(self) -> dict[str, Any]:
        return self._read_version_payload(
            (self.project_root / "version.json").read_bytes()
        )

    def _remote_version(self) -> dict[str, Any]:
        request = Request(
            REMOTE_VERSION_URL,
            headers={"Accept": "application/json", "User-Agent": "kemo-gateway-version-check"},
        )
        with urlopen(request, timeout=4.0) as response:
            return self._read_version_payload(response.read(64 * 1024 + 1))

    def version_check(self) -> dict[str, Any]:
        checked_at = _utc_now()
        try:
            local = self._local_version()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return {
                "status": "unavailable",
                "update_available": None,
                "local": None,
                "remote": None,
                "source": REMOTE_VERSION_URL,
                "checked_at": checked_at,
                "message": "无法读取本地 version.json",
            }

        try:
            remote = self._remote_version()
            local_key = self._version_key(local["version"])
            remote_key = self._version_key(remote["version"])
        except (OSError, HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError, ValueError):
            return {
                "status": "unavailable",
                "update_available": None,
                "local": local,
                "remote": None,
                "source": REMOTE_VERSION_URL,
                "checked_at": checked_at,
                "message": "无法连接远程版本源，请稍后重试",
            }

        update_available = remote_key > local_key
        if update_available:
            status = "update_available"
            message = f"发现新版本 {remote['version']}"
        elif remote_key == local_key:
            status = "up_to_date"
            message = "当前已是最新版本"
        else:
            status = "local_newer"
            message = "当前本地版本高于远程版本"
        return {
            "status": status,
            "update_available": update_available,
            "local": local,
            "remote": remote,
            "source": REMOTE_VERSION_URL,
            "checked_at": checked_at,
            "message": message,
        }

    def cached_version_check(self, *, ttl_seconds: float = 300.0) -> dict[str, Any]:
        """Bound polling traffic to the remote release source."""
        now = time.monotonic()
        with self._version_cache_lock:
            if self._version_cache is not None and now < self._version_cache_until:
                return dict(self._version_cache)
            result = self.version_check()
            self._version_cache = dict(result)
            self._version_cache_until = now + max(1.0, ttl_seconds)
            return result


__all__ = ["REMOTE_VERSION_URL", "SystemInspector"]
