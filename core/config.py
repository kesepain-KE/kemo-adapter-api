"""网关级配置；厂商私有配置由对应 Provider 包自行解析。"""

from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PrincipalConfig:
    tenant_id: str
    subject_id: str
    scopes: frozenset[str] = frozenset({"model:invoke", "asset:read", "asset:write"})
    key_id: str | None = None


def safe_key_id(token: str, configured: object = None) -> str:
    """Return an operator-friendly id or an irreversible legacy fingerprint."""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return f"key_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 7531
    protocol_version: str = "1.0"
    api_keys: dict[str, PrincipalConfig] = field(default_factory=dict)
    provider_settings: dict[str, Any] = field(default_factory=dict)
    web_username: str = ""
    web_password: str = ""
    web_token: str = ""
    status_token: str = ""
    statistics_timezone: str = "Asia/Shanghai"

    @classmethod
    def from_env(cls) -> "Settings":
        api_keys: dict[str, PrincipalConfig] = {}
        raw_keys = os.getenv("GATEWAY_API_KEYS_JSON", "")
        if raw_keys:
            for token, value in json.loads(raw_keys).items():
                api_keys[token] = PrincipalConfig(
                    tenant_id=value["tenant_id"],
                    subject_id=value["subject_id"],
                    scopes=frozenset(value.get("scopes", ["model:invoke"])),
                    key_id=safe_key_id(token, value.get("key_id")),
                )
        legacy_key = os.getenv("GATEWAY_API_KEY")
        if legacy_key and legacy_key not in api_keys:
            api_keys[legacy_key] = PrincipalConfig(
                tenant_id=os.getenv("GATEWAY_DEFAULT_TENANT", "default"),
                subject_id=os.getenv("GATEWAY_DEFAULT_SUBJECT", "kemo-agent"),
                key_id=safe_key_id(legacy_key, os.getenv("GATEWAY_API_KEY_ID")),
            )
        provider_settings = json.loads(os.getenv("PROVIDER_SETTINGS_JSON", "{}"))
        return cls(
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "7531")),
            api_keys=api_keys,
            provider_settings=provider_settings,
            web_username=os.getenv("WEB_USERNAME", ""),
            web_password=os.getenv("WEB_PASSWORD", ""),
            web_token=os.getenv("WEB_TOKEN", ""),
            status_token=os.getenv("STATUS_TOKEN", ""),
            statistics_timezone=os.getenv("STATISTICS_TIMEZONE", "Asia/Shanghai"),
        )
