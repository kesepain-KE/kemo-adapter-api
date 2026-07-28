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
SENSITIVE_CONFIG_KEY = re.compile(
    r"(^|_)(api_?key|token|secret|password|authorization|credential)(_|$)",
    re.IGNORECASE,
)
HEADER_CONFIG_KEYS = frozenset({"default_headers", "headers"})


class RevisionConflict(Exception):
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
        current_config: dict[str, Any] = {}
        if config_path.exists():
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                current_config = parsed
        for header_key in HEADER_CONFIG_KEYS:
            incoming_headers = config.get(header_key)
            existing_headers = current_config.get(header_key)
            if isinstance(incoming_headers, dict) and isinstance(existing_headers, dict):
                config[header_key] = {
                    name: existing_headers.get(name) if value == "" and name in existing_headers else value
                    for name, value in incoming_headers.items()
                }
        self._atomic_json(config_path, config)
        if api_key is not None:
            secrets_path = directory / "secrets.json"
            current: dict[str, Any] = {}
            if secrets_path.exists():
                parsed = json.loads(secrets_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    current = parsed
            current["api_key"] = api_key
            self._atomic_json(secrets_path, current)
