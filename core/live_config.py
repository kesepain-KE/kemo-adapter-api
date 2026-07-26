"""无需重启的运行时控制面配置。

只允许热更新四类内容：
1. 厂商 API 配置；2. 网关 API 配置；3. 最高权限系统提示词；
4. 厂商/模型启停策略。其他配置（尤其环境变量）仍是启动时配置。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.config import PrincipalConfig, safe_key_id


MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LiveConfigSnapshot:
    revision: str = "empty"
    gateway_api: dict[str, Any] = field(default_factory=dict)
    api_keys: dict[str, PrincipalConfig] = field(default_factory=dict)
    gateway_system_prompt: str = ""
    disabled_providers: frozenset[str] = frozenset()
    disabled_models: frozenset[str] = frozenset()
    provider_settings: dict[str, dict[str, Any]] = field(default_factory=dict)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class LiveConfigManager:
    """按内容摘要刷新配置；无效写入不会替换最后一个有效快照。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._lock = asyncio.Lock()
        self._snapshot = LiveConfigSnapshot()
        self._source_fingerprint: tuple[tuple[str, int, int], ...] = ()
        self.last_error: str | None = None

    @property
    def current(self) -> LiveConfigSnapshot:
        return self._snapshot

    def _source_paths(self) -> list[Path]:
        paths = [
            self.project_root / "api" / "runtime.json",
            self.project_root / "api" / "keys.json",
            self.project_root / "core" / "live_control.json",
        ]
        providers_root = self.project_root / "providers"
        if providers_root.exists():
            for directory in providers_root.iterdir():
                if directory.is_dir() and not directory.name.startswith("_"):
                    paths.extend((directory / "config.json", directory / "secrets.json"))
        return paths

    def _fingerprint(self, paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
        values: list[tuple[str, int, int]] = []
        for path in paths:
            if path.exists():
                stat = path.stat()
                values.append((str(path), stat.st_mtime_ns, stat.st_size))
            else:
                values.append((str(path), -1, -1))
        return tuple(values)

    @staticmethod
    def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
        if not path.exists():
            return {}, b""
        size = path.stat().st_size
        if size > MAX_CONFIG_BYTES:
            raise ValueError(f"配置文件超过 {MAX_CONFIG_BYTES} bytes: {path.name}")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"配置顶层必须为 object: {path.name}")
        return value, raw

    async def refresh(self) -> LiveConfigSnapshot:
        paths = self._source_paths()
        fingerprint = self._fingerprint(paths)
        if fingerprint == self._source_fingerprint:
            return self._snapshot

        async with self._lock:
            paths = self._source_paths()
            fingerprint = self._fingerprint(paths)
            if fingerprint == self._source_fingerprint:
                return self._snapshot
            try:
                snapshot = self._load(paths)
            except Exception as exc:
                # 不记录异常正文，避免无意泄漏 secrets.json 内容。
                self.last_error = f"{type(exc).__name__}: live config reload rejected"
                self._source_fingerprint = fingerprint
                return self._snapshot
            self._snapshot = snapshot
            self._source_fingerprint = fingerprint
            self.last_error = None
            return snapshot

    def _load(self, paths: list[Path]) -> LiveConfigSnapshot:
        contents: dict[Path, tuple[dict[str, Any], bytes]] = {
            path: self._read_json(path) for path in paths
        }
        api_runtime = contents[self.project_root / "api" / "runtime.json"][0]
        api_keys_raw = contents[self.project_root / "api" / "keys.json"][0]
        control = contents[self.project_root / "core" / "live_control.json"][0]

        keys: dict[str, PrincipalConfig] = {}
        raw_key_map = api_keys_raw.get("keys", {})
        if not isinstance(raw_key_map, dict):
            raise ValueError("api/keys.json: keys 必须是 object")
        for token, value in raw_key_map.items():
            if not isinstance(token, str) or not token or not isinstance(value, dict):
                raise ValueError("api/keys.json 包含无效 key")
            keys[token] = PrincipalConfig(
                tenant_id=str(value["tenant_id"]),
                subject_id=str(value["subject_id"]),
                scopes=frozenset(value.get("scopes", ["model:invoke"])),
                key_id=safe_key_id(token, value.get("key_id")),
            )

        provider_settings: dict[str, dict[str, Any]] = {}
        providers_root = self.project_root / "providers"
        if providers_root.exists():
            for directory in providers_root.iterdir():
                if not directory.is_dir() or directory.name.startswith("_"):
                    continue
                config = contents.get(directory / "config.json", ({}, b""))[0]
                secrets = contents.get(directory / "secrets.json", ({}, b""))[0]
                provider_settings[directory.name] = _merge(config, secrets)

        gateway_api = api_runtime.get("gateway_api", {})
        if not isinstance(gateway_api, dict):
            raise ValueError("api/runtime.json: gateway_api 必须是 object")
        disabled_providers = control.get("disabled_providers", [])
        disabled_models = control.get("disabled_models", [])
        if not isinstance(disabled_providers, list) or not isinstance(disabled_models, list):
            raise ValueError("core/live_control.json: disabled_* 必须是 array")
        system_prompt = control.get("highest_priority_system_prompt", "")
        if not isinstance(system_prompt, str):
            raise ValueError("highest_priority_system_prompt 必须是 string")

        return LiveConfigSnapshot(
            # revision 不从密钥内容派生，避免把 secrets 的摘要暴露为可探测值。
            revision=f"live_{uuid4().hex}",
            gateway_api=gateway_api,
            api_keys=keys,
            gateway_system_prompt=system_prompt,
            disabled_providers=frozenset(str(value) for value in disabled_providers),
            disabled_models=frozenset(str(value) for value in disabled_models),
            provider_settings=provider_settings,
        )
