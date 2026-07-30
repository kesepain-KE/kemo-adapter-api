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
    # None means all models are allowed; an empty set means deny all.
    allowed_models: frozenset[str] | None = None


def safe_key_id(token: str, configured: object = None) -> str:
    """Return an operator-friendly id or an irreversible legacy fingerprint."""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return f"key_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 7531
    base_url: str = ""
    protocol_version: str = "1.0"
    api_keys: dict[str, PrincipalConfig] = field(default_factory=dict)
    provider_settings: dict[str, Any] = field(default_factory=dict)
    web_username: str = ""
    web_password: str = ""
    web_token: str = ""
    web_cookie_secure: bool | None = None
    web_allowed_hosts: tuple[str, ...] = ()
    api_docs_enabled: bool = False
    status_token: str = ""
    statistics_timezone: str = "Asia/Shanghai"
    request_json_max_bytes: int = 2 * 1024 * 1024
    asset_retention_hours: int = 24
    asset_image_max_bytes: int = 20 * 1024 * 1024
    asset_audio_max_bytes: int = 100 * 1024 * 1024
    asset_video_max_bytes: int = 1024 * 1024 * 1024
    asset_file_max_bytes: int = 100 * 1024 * 1024
    model_execution_timeout_seconds: float = 900.0
    max_concurrent_executions: int = 64
    sse_heartbeat_seconds: float = 15.0
    execution_retention_hours: int = 24
    max_sse_events_per_response: int = 200_000

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
                    allowed_models=parse_allowed_models(value.get("allowed_models")),
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
            base_url=os.getenv("GATEWAY_BASE_URL", "").strip(),
            api_keys=api_keys,
            provider_settings=provider_settings,
            web_username=os.getenv("WEB_USERNAME", ""),
            web_password=os.getenv("WEB_PASSWORD", ""),
            web_token=os.getenv("WEB_TOKEN", ""),
            web_cookie_secure=_optional_bool_env("WEB_COOKIE_SECURE"),
            web_allowed_hosts=_csv_env("WEB_ALLOWED_HOSTS"),
            api_docs_enabled=_bool_env("API_DOCS_ENABLED", False),
            status_token=os.getenv("STATUS_TOKEN", ""),
            statistics_timezone=os.getenv("STATISTICS_TIMEZONE", "Asia/Shanghai"),
            request_json_max_bytes=_positive_int_env(
                "REQUEST_JSON_MAX_BYTES", 2 * 1024 * 1024
            ),
            asset_retention_hours=_positive_int_env("DEFAULT_ASSET_TTL_HOURS", 24),
            asset_image_max_bytes=_positive_int_env(
                "ASSET_IMAGE_MAX_BYTES", 20 * 1024 * 1024
            ),
            asset_audio_max_bytes=_positive_int_env(
                "ASSET_AUDIO_MAX_BYTES", 100 * 1024 * 1024
            ),
            asset_video_max_bytes=_positive_int_env(
                "ASSET_VIDEO_MAX_BYTES", 1024 * 1024 * 1024
            ),
            asset_file_max_bytes=_positive_int_env(
                "ASSET_FILE_MAX_BYTES", 100 * 1024 * 1024
            ),
            model_execution_timeout_seconds=_positive_float_env(
                "MODEL_EXECUTION_TIMEOUT_SECONDS", 900.0
            ),
            max_concurrent_executions=_positive_int_env(
                "MAX_CONCURRENT_EXECUTIONS", 64
            ),
            sse_heartbeat_seconds=_positive_float_env(
                "SSE_HEARTBEAT_SECONDS", 15.0
            ),
            execution_retention_hours=_positive_int_env(
                "EXECUTION_RETENTION_HOURS", 24
            ),
            max_sse_events_per_response=_positive_int_env(
                "MAX_SSE_EVENTS_PER_RESPONSE", 200_000
            ),
        )


def parse_allowed_models(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("allowed_models 必须是数组或 null")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("allowed_models 只能包含非空模型名")
    return frozenset(item.strip() for item in value)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} 必须为正数")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false、1/0、yes/no 或 on/off")


def _optional_bool_env(name: str) -> bool | None:
    raw = os.getenv(name, "").strip().lower()
    if not raw or raw == "auto":
        return None
    return _bool_env(name, False)


def _csv_env(name: str) -> tuple[str, ...]:
    values = tuple(
        value.strip() for value in os.getenv(name, "").split(",") if value.strip()
    )
    if any("/" in value or ":" in value for value in values):
        raise ValueError(f"{name} 只能包含主机名，不能包含协议、端口或路径")
    return values
