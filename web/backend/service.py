"""管理端运行时配置写入服务。只允许写四类无需重启的配置。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.config import safe_key_id


PROVIDER_ID = re.compile(r"^[a-z0-9_]+$")
PROVIDER_KEY_ID = re.compile(r"^[A-Za-z0-9._-]+$")
SENSITIVE_CONFIG_KEY = re.compile(
    r"(^|_)(api_?key|token|secret|password|authorization|credential)(_|$)",
    re.IGNORECASE,
)
HEADER_CONFIG_KEYS = frozenset({"default_headers", "headers"})


class RevisionConflict(Exception):
    pass


class ProviderKeyPoolConflict(Exception):
    pass


class RuntimeConfigWriter:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.lock = asyncio.Lock()

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _atomic_bytes(path: Path, value: bytes) -> None:
        """Restore a previously captured file without parsing its contents."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(value)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _managed_path(self, path: Path) -> Path:
        resolved = path.resolve()
        allowed_roots = (
            self.project_root / "api",
            self.project_root / "core",
            self.project_root / "providers",
        )
        if not any(
            resolved == root or root in resolved.parents for root in allowed_roots
        ):
            raise ValueError("不允许操作项目外的运行时文件")
        return resolved

    def capture_files(self, paths: list[Path]) -> dict[Path, bytes | None]:
        """Capture exact bytes for a small, known set of runtime files.

        The Web control plane uses this before a write so a failed hot reload
        can restore the exact previous representation (including legacy fields
        that are intentionally not exposed to the browser).
        """
        snapshot: dict[Path, bytes | None] = {}
        for path in paths:
            resolved = self._managed_path(path)
            if resolved.exists():
                if not resolved.is_file():
                    raise ValueError(f"运行时配置路径不是文件: {resolved.name}")
                snapshot[resolved] = resolved.read_bytes()
            else:
                snapshot[resolved] = None
        return snapshot

    def restore_files(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore a snapshot captured by :meth:`capture_files`."""
        for path, content in snapshot.items():
            resolved = self._managed_path(path)
            if content is None:
                if resolved.exists():
                    resolved.unlink()
            else:
                self._atomic_bytes(resolved, content)

    @staticmethod
    def assert_revision(expected: str, current: str) -> None:
        if expected != current:
            raise RevisionConflict("运行时配置已被其他操作者更新，请刷新后重试")

    def update_gateway(self, *, enabled: bool) -> None:
        self._atomic_json(
            self.project_root / "api" / "runtime.json",
            {"gateway_api": {"enabled": enabled}},
        )

    def update_key_model_policy(
        self, key_id: str, *, allowed_models: list[str] | None
    ) -> None:
        path = self.project_root / "api" / "keys.json"
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LookupError("运行时密钥配置不存在") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), dict):
            raise ValueError("api/keys.json: keys 必须是 object")

        matches: list[dict[str, Any]] = []
        for token, value in parsed["keys"].items():
            if not isinstance(token, str) or not isinstance(value, dict):
                raise ValueError("api/keys.json 包含无效 key")
            if safe_key_id(token, value.get("key_id")) == key_id:
                matches.append(value)
        if not matches:
            raise LookupError("只能修改 api/keys.json 中的运行时密钥")
        if len(matches) > 1:
            raise ValueError("api/keys.json 中存在重复 key_id")

        if allowed_models is None:
            matches[0]["allowed_models"] = None
        else:
            normalized = [model.strip() for model in allowed_models]
            if any(not model for model in normalized):
                raise ValueError("allowed_models 不能包含空模型名")
            matches[0]["allowed_models"] = sorted(set(normalized))
        self._atomic_json(path, parsed)

    def provider_configs(self) -> dict[str, dict[str, Any]]:
        """只返回非密钥配置；secrets.json 永远不进入浏览器。"""
        result: dict[str, dict[str, Any]] = {}
        providers_root = self.project_root / "providers"
        if not providers_root.exists():
            return result
        for directory in providers_root.iterdir():
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            path = directory / "config.json"
            if not path.exists():
                result[directory.name] = {}
                continue
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                result[directory.name] = {}
                continue
            if isinstance(parsed, dict):
                result[directory.name] = self._public_config(parsed)
        return result

    @classmethod
    def _public_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        public: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if SENSITIVE_CONFIG_KEY.search(normalized_key):
                continue
            if normalized_key in HEADER_CONFIG_KEYS and isinstance(item, dict):
                # Header values can carry arbitrary credentials. Only names may reach the browser.
                public[key] = {str(name): "" for name in item}
            elif isinstance(item, dict):
                public[key] = cls._public_config(item)
            elif isinstance(item, list):
                public[key] = [
                    cls._public_config(entry) if isinstance(entry, dict) else entry
                    for entry in item
                ]
            else:
                public[key] = item
        return public

    def update_control(
        self,
        *,
        prompt: str,
        disabled_providers: list[str],
        disabled_models: list[str],
    ) -> None:
        self._atomic_json(
            self.project_root / "core" / "live_control.json",
            {
                "highest_priority_system_prompt": prompt,
                "disabled_providers": sorted(set(disabled_providers)),
                "disabled_models": sorted(set(disabled_models)),
            },
        )

    @staticmethod
    def _canonical_provider_secrets(
        current: dict[str, Any], entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return secrets with one canonical ordered ``api_keys`` field.

        ``api_key`` and ``api_key_id`` were accepted by older Provider
        packages, but keeping both representations creates two sources of
        truth.  We retain unrelated Provider secret fields while removing the
        legacy aliases whenever the Web control plane writes the file.
        """

        if not entries:
            raise ValueError("密钥池不能为空")
        canonical = dict(current)
        canonical.pop("api_key", None)
        canonical.pop("api_key_id", None)
        canonical["api_keys"] = [dict(entry) for entry in entries]
        return canonical

    def update_provider(
        self, provider_id: str, *, config: dict[str, Any], api_key: str | None
    ) -> None:
        if not PROVIDER_ID.fullmatch(provider_id) or provider_id.startswith("_"):
            raise ValueError("Provider ID 无效")
        directory = (self.project_root / "providers" / provider_id).resolve()
        providers_root = (self.project_root / "providers").resolve()
        if directory.parent != providers_root or not directory.is_dir():
            raise LookupError("Provider 目录不存在；新增 Provider 代码需要重启部署")
        config_path = directory / "config.json"
        secrets_path = directory / "secrets.json"
        current_config: dict[str, Any] = {}
        if config_path.exists():
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("Provider config.json 顶层必须是 object")
            current_config = parsed
        candidate_config = dict(config)
        for header_key in HEADER_CONFIG_KEYS:
            incoming_headers = candidate_config.get(header_key)
            existing_headers = current_config.get(header_key)
            if isinstance(incoming_headers, dict) and isinstance(existing_headers, dict):
                candidate_config[header_key] = {
                    name: existing_headers.get(name) if value == "" and name in existing_headers else value
                    for name, value in incoming_headers.items()
                }

        # Read and validate every file before changing either one.  A malformed
        # secrets file or a blank replacement key must never leave config.json
        # half-updated.
        current_secrets: dict[str, Any] = {}
        if api_key is not None:
            if secrets_path.exists():
                parsed = json.loads(secrets_path.read_text(encoding="utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("Provider secrets.json 顶层必须是 object")
                current_secrets = parsed
            secret = api_key.strip()
            if not secret:
                raise ValueError("密钥不能为空")
            # This endpoint is the explicit single-key initializer/replacer.
            # The Provider page owns multi-key pools and does not call it once
            # a pool exists, so replacing the pool here preserves the old
            # single ``api_key`` semantics without leaving stale backups.
            existing = [{"key_id": "primary", "api_key": secret, "enabled": True}]
            candidate_secrets = self._canonical_provider_secrets(current_secrets, existing)
        else:
            candidate_secrets = None

        before = self.capture_files(
            [config_path, secrets_path] if candidate_secrets is not None else [config_path]
        )
        try:
            self._atomic_json(config_path, candidate_config)
            if candidate_secrets is not None:
                self._atomic_json(
                    secrets_path,
                    candidate_secrets,
                )
        except Exception:
            self.restore_files(before)
            raise

    def update_provider_keys(self, provider_id: str, *, keys: list[dict[str, Any]]) -> None:
        """Replace one Provider's ordered upstream key pool atomically."""
        if not PROVIDER_ID.fullmatch(provider_id) or provider_id.startswith("_"):
            raise ValueError("Provider ID 无效")
        directory = (self.project_root / "providers" / provider_id).resolve()
        providers_root = (self.project_root / "providers").resolve()
        if directory.parent != providers_root or not directory.is_dir():
            raise LookupError("Provider 目录不存在")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in keys:
            key_id = str(entry.get("key_id") or "").strip()
            api_key = str(entry.get("api_key") or "").strip()
            enabled = entry.get("enabled", True)
            if (
                not PROVIDER_KEY_ID.fullmatch(key_id)
                or not api_key
                or key_id in seen
            ):
                raise ValueError("密钥标识必须唯一且密钥不能为空")
            if not isinstance(enabled, bool):
                raise ValueError("密钥 enabled 必须是布尔值")
            seen.add(key_id)
            normalized.append({"key_id": key_id, "api_key": api_key, "enabled": enabled})
        if not any(entry["enabled"] for entry in normalized):
            raise ValueError("Provider 至少保留一个启用的上游密钥；如需停用，请使用 Provider 开关")
        secrets_path = directory / "secrets.json"
        current: dict[str, Any] = {}
        if secrets_path.exists():
            parsed = json.loads(secrets_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                current = parsed
        self._atomic_json(
            secrets_path,
            self._canonical_provider_secrets(current, normalized),
        )

    @staticmethod
    def _existing_provider_keys(current: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize an existing secrets file without exposing or dropping keys."""

        raw = current.get("api_keys")
        if raw is None:
            raw_entries: list[Any] = []
        elif isinstance(raw, list):
            raw_entries = raw
        elif isinstance(raw, dict):
            raw_entries = [
                ({**value, "key_id": key_id} if isinstance(value, dict) else {"key_id": key_id, "api_key": value})
                for key_id, value in raw.items()
            ]
        else:
            raise ValueError("secrets.json: api_keys 必须是数组或对象")

        # Older Provider packages use one legacy api_key field.
        if raw is None and not raw_entries:
            legacy = str(current.get("api_key") or "").strip()
            if legacy:
                raw_entries = [
                    {
                        "key_id": str(current.get("api_key_id") or "primary").strip(),
                        "api_key": legacy,
                        "enabled": True,
                    }
                ]

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw_entries):
            if isinstance(entry, str):
                key_id, api_key, enabled = f"key-{index + 1}", entry.strip(), True
            elif isinstance(entry, dict):
                key_id = str(entry.get("key_id") or entry.get("name") or f"key-{index + 1}").strip()
                api_key = str(entry.get("api_key") or entry.get("key") or "").strip()
                enabled = entry.get("enabled", True)
            else:
                raise ValueError("secrets.json: api_keys 包含无效项")
            if not PROVIDER_KEY_ID.fullmatch(key_id) or not api_key:
                raise ValueError("secrets.json: 密钥标识和密钥不能为空")
            if not isinstance(enabled, bool):
                raise ValueError("secrets.json: enabled 必须是布尔值")
            if key_id in seen:
                raise ValueError("secrets.json: 密钥标识必须唯一")
            seen.add(key_id)
            normalized.append({"key_id": key_id, "api_key": api_key, "enabled": enabled})
        return normalized

    def append_provider_key(self, provider_id: str, *, api_key: str) -> None:
        """Append one upstream key while preserving the existing pool atomically."""

        if not PROVIDER_ID.fullmatch(provider_id) or provider_id.startswith("_"):
            raise ValueError("Provider ID 无效")
        directory = (self.project_root / "providers" / provider_id).resolve()
        providers_root = (self.project_root / "providers").resolve()
        if directory.parent != providers_root or not directory.is_dir():
            raise LookupError("Provider 目录不存在")
        secret = api_key.strip()
        if not secret:
            raise ValueError("密钥不能为空")
        secrets_path = directory / "secrets.json"
        current: dict[str, Any] = {}
        if secrets_path.exists():
            parsed = json.loads(secrets_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("secrets.json 必须是 object")
            current = parsed
        normalized = self._existing_provider_keys(current)
        if any(entry["api_key"] == secret for entry in normalized):
            raise ValueError("该上游密钥已经存在")
        used_ids = {entry["key_id"] for entry in normalized}
        if not normalized:
            key_id = "primary"
        else:
            suffix = 1
            key_id = f"backup-{suffix}"
            while key_id in used_ids:
                suffix += 1
                key_id = f"backup-{suffix}"
        normalized.append({"key_id": key_id, "api_key": secret, "enabled": True})
        self._atomic_json(
            secrets_path,
            self._canonical_provider_secrets(current, normalized),
        )

    def remove_provider_key(self, provider_id: str, *, key_id: str) -> None:
        """Remove one upstream key, never allowing an empty Provider pool."""

        if not PROVIDER_ID.fullmatch(provider_id) or provider_id.startswith("_"):
            raise ValueError("Provider ID 无效")
        if not PROVIDER_KEY_ID.fullmatch(key_id):
            raise ValueError("密钥标识无效")
        directory = (self.project_root / "providers" / provider_id).resolve()
        providers_root = (self.project_root / "providers").resolve()
        if directory.parent != providers_root or not directory.is_dir():
            raise LookupError("Provider 目录不存在")
        secrets_path = directory / "secrets.json"
        current: dict[str, Any] = {}
        if secrets_path.exists():
            parsed = json.loads(secrets_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("secrets.json 必须是 object")
            current = parsed
        normalized = self._existing_provider_keys(current)
        if len(normalized) <= 1:
            raise ProviderKeyPoolConflict("Provider 至少保留一个上游密钥，无法删除最后一个密钥")
        remaining = [entry for entry in normalized if entry["key_id"] != key_id]
        if len(remaining) == len(normalized):
            raise LookupError("上游密钥不存在")
        if not any(entry["enabled"] for entry in remaining):
            raise ProviderKeyPoolConflict(
                "删除后 Provider 将没有启用的上游密钥；请先启用或追加另一个密钥"
            )
        self._atomic_json(
            secrets_path,
            self._canonical_provider_secrets(current, remaining),
        )
